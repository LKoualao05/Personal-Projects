"""Gmail search + message fetching (read-only)."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from dateutil import tz

from .schemas import EmailMessage

# googleapiclient retries 429/5xx and 403 rate-limit errors with exponential backoff.
# Gmail enforces a per-user units-per-minute quota that a large backlog hits quickly.
API_RETRIES = 8

# Queries that surface confirmations *and* later-stage emails (rejections,
# interviews, assessments, offers) so Gemini can track status changes.
CANDIDATE_QUERIES = [
    '("application received" OR "application submitted" OR "submission received" OR "application confirmation")',
    '("thanks for applying" OR "thank you for applying" OR "thanks for your application" OR "thank you for your application")',
    '("we received your application" OR "received your application" OR "your application has been received")',
    '("successfully applied" OR "successfully submitted" OR "application complete" OR "submission complete")',
    '("your application to" OR "your application for" OR "confirming your application")',
    "(from:noreply OR from:no-reply OR from:careers OR from:recruiting OR from:talent OR from:jobs) (application OR applied OR submit)",
    "(from:greenhouse OR from:workday OR from:myworkday OR from:lever OR from:smartrecruiters OR from:icims OR from:successfactors OR from:ashbyhq)",
    "subject:(application OR applied OR submission) (received OR submitted OR confirmation OR thank)",
    '("your profile has been submitted" OR "profile submitted" OR "candidate profile" OR "added to our candidate pool")',
    '("application is being reviewed" OR "reviewing your application" OR "your candidacy")',
    # Later-stage emails
    '("regret to inform" OR "not moving forward" OR "other candidates" OR "decided not to proceed" OR "position has been filled") (application OR position OR role OR internship)',
    '(interview OR "phone screen" OR "next steps" OR "schedule a call") (application OR position OR role OR internship)',
    '(hackerrank OR codesignal OR codility OR hirevue OR "online assessment" OR "coding challenge" OR "take-home")',
    '("offer letter" OR "pleased to offer" OR "excited to offer" OR "offer of employment")',
]


def search_candidate_messages(gmail, after: str, queries: Iterable[str] = CANDIDATE_QUERIES, log=print) -> list[str]:
    ids: set[str] = set()
    for q in queries:
        query = f"after:{after} {q}"
        page_token = None
        try:
            while True:
                res = gmail.users().messages().list(userId="me", q=query, pageToken=page_token, maxResults=500).execute(num_retries=API_RETRIES)
                ids.update(m["id"] for m in res.get("messages", []))
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:  # keep going if one query fails
            log(f"Warning: Gmail query failed ({q[:50]}...): {exc}")
    return sorted(ids)


def search_messages(gmail, query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Free-form Gmail search returning lightweight metadata (used by the MCP server)."""
    res = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute(num_retries=API_RETRIES)
    out = []
    for m in res.get("messages", []):
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"]
        ).execute(num_retries=API_RETRIES)
        out.append({
            "id": msg["id"],
            "thread_id": msg.get("threadId", ""),
            "subject": get_header(msg, "Subject"),
            "from": get_header(msg, "From"),
            "date": get_header(msg, "Date"),
            "snippet": msg.get("snippet", ""),
        })
    return out


def fetch_message(gmail, message_id: str) -> EmailMessage:
    msg = gmail.users().messages().get(userId="me", id=message_id, format="full").execute(num_retries=API_RETRIES)
    return parse_message(msg)


def parse_message(msg: dict[str, Any]) -> EmailMessage:
    payload = msg.get("payload", {})
    plain, html = _collect_parts(payload)
    body = plain
    if html and len(plain) < 200:  # plain part is often just "view in browser"
        body = html_to_text(html)
    body = normalize_body(body or msg.get("snippet", ""))
    return EmailMessage(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        internal_date_ms=int(msg.get("internalDate", 0)),
        subject=get_header(msg, "Subject"),
        sender=get_header(msg, "From"),
        snippet=msg.get("snippet", ""),
        body=body,
        links=extract_links(html or plain),
    )


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")


def _collect_parts(payload: dict[str, Any]) -> tuple[str, str]:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(plain).strip(), "\n".join(html).strip()


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text(" ")


def normalize_body(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v ‌​]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def extract_links(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for url in _URL_RE.findall(text or ""):
        seen.setdefault(url.rstrip(".,;"), None)
    return list(seen)


def get_header(msg: dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_from_name(from_header: str) -> str:
    m = re.match(r'"?([^"<]+)"?\s*<[^>]+>', from_header or "")
    return re.sub(r"\s+", " ", m.group(1) if m else (from_header or "")).strip()


def build_thread_url(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"


def iso_date_from_internal(ts_ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return dt.astimezone(tz.gettz(tz_name)).date().isoformat()
