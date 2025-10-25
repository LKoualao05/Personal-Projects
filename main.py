from __future__ import annotations
import os, json, base64, re
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from dateutil import tz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from gmail_search import contains_confirmation, extract_company, extract_job_id, extract_role, gmail_query_since, normalize_space
from sheets_io import ensure_sheet_and_headers, get_processed_ids, append_processed_ids, append_applications, refresh_company_summary, APPLICATION_HEADERS

# SCOPES: Gmail read-only + Sheets read/write
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets"
]

def load_config() -> Dict[str, Any]:
    with open("config.json", "r") as f:
        return json.load(f)

def get_creds() -> Credentials:
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds

def build_services(creds: Credentials):
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return gmail, sheets

def decode_snippet(payload: Dict[str, Any]) -> str:
    try:
        data = payload.get("snippet", "")
        return data or ""
    except Exception:
        return ""

def extract_body_content(message: Dict[str, Any]) -> str:
    """Extract text content from Gmail message body"""
    def get_text_from_payload(payload):
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                import base64
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
        elif payload.get("mimeType") == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                import base64
                html_content = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
                # Simple HTML tag removal for basic text extraction
                import re
                text = re.sub(r'<[^>]+>', ' ', html_content)
                text = re.sub(r'\s+', ' ', text).strip()
                return text
        elif payload.get("mimeType", "").startswith("multipart/"):
            parts = payload.get("parts", [])
            for part in parts:
                text = get_text_from_payload(part)
                if text:
                    return text
        return ""
    
    try:
        return get_text_from_payload(message.get("payload", {}))
    except Exception:
        return ""

def get_header(msg: Dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name") == name:
            return h.get("value", "")
    return ""

def parse_from_header(from_header: str) -> str:
    # Extract display name before <email>
    m = re.match(r'\"?([^\"<]+)\"?\s*<[^>]+>', from_header)
    if m:
        return normalize_space(m.group(1))
    return normalize_space(from_header)

def build_thread_url(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"

def iso_date_from_internal(ts_ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ts_ms/1000.0, tz=timezone.utc)
    local = dt.astimezone(tz.gettz(tz_name))
    return local.date().isoformat()

def search_confirmation_messages(gmail, lookback_days: int) -> List[str]:
    base_query = gmail_query_since(lookback_days)
    
    # Cast a much wider net with multiple search strategies
    search_queries = [
        # Primary confirmation searches
        f'{base_query} ("application received" OR "application submitted" OR "submission received" OR "application confirmation")',
        f'{base_query} ("thanks for applying" OR "thank you for applying" OR "thanks for your application" OR "thank you for your application")',
        f'{base_query} ("we received your application" OR "received your application" OR "your application has been received")',
        f'{base_query} ("successfully applied" OR "successfully submitted" OR "application complete" OR "submission complete")',
        f'{base_query} ("your application to" OR "your application for" OR "confirming your application")',
        
        # Broader searches for common senders
        f'{base_query} (from:noreply OR from:no-reply OR from:careers OR from:recruiting OR from:talent OR from:jobs) (application OR applied OR submit)',
        f'{base_query} (from:greenhouse OR from:workday OR from:lever OR from:smartrecruiters OR from:icims OR from:successfactors)',
        
        # Subject line patterns
        f'{base_query} subject:(application OR applied OR submission) (received OR submitted OR confirmation OR thank)',
        
        # Company-specific patterns
        f'{base_query} ("your profile has been submitted" OR "profile submitted" OR "candidate profile" OR "added to our candidate pool")',
        f'{base_query} ("application is being reviewed" OR "reviewing your application" OR "your candidacy")',
    ]
    
    all_ids = set()  # Use set to avoid duplicates
    
    for query in search_queries:
        try:
            page_token = None
            while True:
                res = gmail.users().messages().list(
                    userId="me", 
                    q=query, 
                    pageToken=page_token, 
                    maxResults=500
                ).execute()
                
                messages = res.get("messages", [])
                all_ids.update(m["id"] for m in messages)
                
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            # Continue with other queries if one fails
            print(f"Warning: Search query failed: {query[:50]}... Error: {e}")
            continue
    
    return list(all_ids)

def main():
    cfg = load_config()
    creds = get_creds()
    gmail, sheets = build_services(creds)

    spreadsheet_id = cfg["SHEET_ID"]
    app_sheet = cfg["SHEET_NAME"]
    processed_sheet = cfg["PROCESSED_SHEET_NAME"]
    summary_sheet = cfg["SUMMARY_SHEET_NAME"]
    timezone_name = cfg["TIMEZONE"]

    # Ensure headers
    ensure_sheet_and_headers(sheets, spreadsheet_id, app_sheet, APPLICATION_HEADERS)
    processed_ids = get_processed_ids(sheets, spreadsheet_id, processed_sheet)

    # Search
    print("Searching for internship application emails...")
    ids = search_confirmation_messages(gmail, cfg["GMAIL_QUERY_WINDOW_DAYS"])
    print(f"Found {len(ids)} potential messages to check")

    new_rows = []
    just_processed = []
    processed_count = 0
    confirmed_count = 0

    for i, mid in enumerate(ids):
        if mid in processed_ids:
            continue
        
        # Progress indicator
        if i % 50 == 0:
            print(f"Processing message {i+1}/{len(ids)}...")
        
        try:
            # First get metadata to check basic criteria
            msg = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject","From"]).execute()
            subject = get_header(msg, "Subject")
            from_header = get_header(msg, "From")
            from_name = parse_from_header(from_header)
            snippet = msg.get("snippet","")
            
            # Try with snippet first
            is_confirmation = contains_confirmation(subject, snippet)
            
            # If snippet check fails but subject looks promising, get full content
            if not is_confirmation and subject:
                subject_lower = subject.lower()
                promising_subject = any(keyword in subject_lower for keyword in [
                    "application", "applied", "submission", "thank", "received", "confirmation"
                ])
                
                if promising_subject:
                    try:
                        # Get full message content
                        full_msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
                        full_body = extract_body_content(full_msg)
                        is_confirmation = contains_confirmation(subject, full_body[:1000])  # Use first 1000 chars
                    except Exception:
                        # Fall back to snippet if full content fails
                        is_confirmation = False
            
            if not is_confirmation:
                continue

            confirmed_count += 1
            thread_id = msg.get("threadId")
            internal_dt_ms = int(msg.get("internalDate"))  # ms epoch
            date_applied = iso_date_from_internal(internal_dt_ms, timezone_name)

            company = extract_company(subject, from_name)
            job_id = extract_job_id(subject, snippet) or ""
            role = extract_role(subject, snippet) or ""

            row = [
                company,
                role,
                job_id,
                date_applied,
                build_thread_url(thread_id),
                from_header,
                subject,
                mid
            ]
            new_rows.append(row)
            just_processed.append(mid)
            
            # Show confirmation found
            print(f"✓ Found application: {company} - {role}")
            
        except HttpError as e:
            print(f"Warning: Could not process message {mid}: {e}")
            continue
        except Exception as e:
            print(f"Warning: Unexpected error processing message {mid}: {e}")
            continue

    print(f"\nProcessing complete!")
    print(f"Found {confirmed_count} internship application confirmations")
    print(f"Added {len(new_rows)} new entries")

    if new_rows:
        append_applications(sheets, spreadsheet_id, app_sheet, new_rows)
    if just_processed:
        append_processed_ids(sheets, spreadsheet_id, processed_sheet, just_processed)

    # Refresh summary
    refresh_company_summary(sheets, spreadsheet_id, app_sheet, summary_sheet)

if __name__ == "__main__":
    main()
