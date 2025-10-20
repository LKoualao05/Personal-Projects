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
    # broaden search with likely confirmation phrases; exclusion handled by our filter
    search_phrases = [
        '"application received" OR "application submitted" OR "thanks for applying" OR "your application to" OR "submission received"'
    ]
    full_query = f"{base_query} ({' '.join(search_phrases)})"
    ids = []
    page_token = None
    while True:
        res = gmail.users().messages().list(userId="me", q=full_query, pageToken=page_token, maxResults=500).execute()
        ids.extend([m["id"] for m in res.get("messages", [])])
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return ids

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
    ids = search_confirmation_messages(gmail, cfg["GMAIL_QUERY_WINDOW_DAYS"])

    new_rows = []
    just_processed = []

    for mid in ids:
        if mid in processed_ids:
            continue
        try:
            msg = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject","From"]).execute()
            subject = get_header(msg, "Subject")
            from_header = get_header(msg, "From")
            from_name = parse_from_header(from_header)
            snippet = msg.get("snippet","")
            if not contains_confirmation(subject, snippet):
                continue

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
        except HttpError as e:
            # skip problematic message but mark as processed to avoid repeated errors? safer to skip without marking
            continue

    if new_rows:
        append_applications(sheets, spreadsheet_id, app_sheet, new_rows)
    if just_processed:
        append_processed_ids(sheets, spreadsheet_id, processed_sheet, just_processed)

    # Refresh summary
    refresh_company_summary(sheets, spreadsheet_id, app_sheet, summary_sheet)

if __name__ == "__main__":
    main()
