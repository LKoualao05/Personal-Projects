import re
from typing import Optional

CONFIRMATION_KEYWORDS = [
    # common confirmation phrases
    "application received",
    "thanks for applying",
    "thank you for applying",
    "we received your application",
    "we've received your application",
    "your application to",
    "application confirmation",
    "application submitted",
    "submission received",
    "we have received your application",
    "your application was received",
    "you successfully applied",
    "you've successfully applied",
    "we confirm your application",
    "application acknowledgment",
    "application acknowledgement",
]

EXCLUSION_KEYWORDS = [
    # exclude updates beyond confirmations
    "assessment",
    "technical assessment",
    "coding challenge",
    "hackerrank",
    "codility",
    "codesignal",
    "interview",
    "phone screen",
    "status update",
    "application status",
    "under review",
    "regret",
    "unfortunately",
    "not selected",
    "we are moving forward",
    "offer",
    "background check",
    "newsletter",
    "talent community",
    "talent network",
    "subscribe",
    "survey",
]

JOB_ID_PATTERNS = [
    r"(?:job\s*id|requisition\s*id|req(?:uisition)?\s*#?|jr|r-)\s*[:#]?\s*([A-Za-z0-9\-_/]+)",
    r"\(?(?:id|job)\s*[:#]?\s*([A-Za-z0-9\-_/]+)\)?",
    r"\b([A-Z]{1,4}-\d{3,8})\b",  # patterns like R-12345, JR-123456, ABC-1234
    r"\b(\d{6,})\b",              # long numeric ids
]

ROLE_PATTERNS = [
    r"(?:for|position(?:\s*title)?|role(?:\s*title)?|applying\s*for)\s*[:\-]?\s*\"?([A-Za-z0-9 \-_/&()+.,']+)\"?",
    r"your application to .*? for ([A-Za-z0-9 \-_/&()+.,']+)",
    r"application received - ([A-Za-z0-9 \-_/&()+.,']+)",
]

COMPANY_PATTERNS = [
    r"your application to ([A-Za-z0-9 .,&'()+\-_/]+)",
    r"application with ([A-Za-z0-9 .,&'()+\-_/]+)",
    r"application received (?:at|by)\s+([A-Za-z0-9 .,&'()+\-_/]+)",
]

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def contains_confirmation(subject: str, body_snippet: str) -> bool:
    text = f"{subject}\n{body_snippet}".lower()
    has_conf = any(k in text for k in CONFIRMATION_KEYWORDS)
    has_excl = any(k in text for k in EXCLUSION_KEYWORDS)
    return has_conf and not has_excl

def extract_company(subject: str, from_header_name: str) -> str:
    subj = subject or ""
    for pat in COMPANY_PATTERNS:
        m = re.search(pat, subj, flags=re.IGNORECASE)
        if m:
            return normalize_space(m.group(1))
    # fallback to From name
    company = from_header_name or ""
    # remove common suffixes
    company = re.sub(r"\b(careers|recruiting|talent acquisition|hiring|noreply|no-reply)\b", "", company, flags=re.I)
    # remove known ats vendor markers
    company = re.sub(r"\b(workday|greenhouse|lever|smartrecruiters|icims|successfactors|bamboohr)\b", "", company, flags=re.I)
    return normalize_space(company)

def extract_job_id(subject: str, body_snippet: str) -> Optional[str]:
    text = f"{subject}\n{body_snippet}"
    for pat in JOB_ID_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return normalize_space(m.group(1))
    return None

def extract_role(subject: str, body_snippet: str) -> Optional[str]:
    text = f"{subject}\n{body_snippet}"
    for pat in ROLE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return normalize_space(m.group(1))
    # heuristic: subject pieces like "Application received - Data Analyst Intern"
    m = re.search(r"application.*?[-:]\s*([A-Za-z0-9 \-_/&()+.,']+)", subject or "", flags=re.I)
    if m:
        return normalize_space(m.group(1))
    return None

def gmail_query_since(days_back: int) -> str:
    # Only search for emails from 2025 or later
    return 'after:2025/01/01'
