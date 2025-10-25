import re
from typing import Optional

CONFIRMATION_KEYWORDS = [
    # Direct confirmation phrases
    "application received",
    "application submitted",
    "application confirmation",
    "submission received",
    "submission confirmed",
    "application acknowledgment",
    "application acknowledgement",
    
    # Thank you variations
    "thanks for applying",
    "thank you for applying",
    "thanks for your application",
    "thank you for your application",
    "thanks for submitting",
    "thank you for submitting",
    "thank you for your interest",
    "thanks for your interest",
    
    # Received variations
    "we received your application",
    "we've received your application",
    "we have received your application",
    "your application was received",
    "received your application for",
    "your application has been received",
    "application successfully received",
    
    # Success confirmations
    "you successfully applied",
    "you've successfully applied",
    "successfully submitted",
    "application complete",
    "submission complete",
    "application was successful",
    
    # Sent/forwarded confirmations (LinkedIn, etc.)
    "your application was sent",
    "application was sent",
    "your application has been sent",
    "application sent to",
    "forwarded your application",
    "application forwarded to",
    
    # Formal confirmations
    "we confirm your application",
    "this confirms your application",
    "confirming your application",
    "application on file",
    "your application to",
    "your application for the position",
    "your application for the role",
    
    # ATS system phrases
    "your profile has been submitted",
    "profile submitted successfully",
    "application in our system",
    "added to our candidate pool",
    "candidate profile received",
    
    # Alternative phrasings
    "application is being reviewed",
    "we'll review your application",
    "reviewing your application",
    "your candidacy",
    "thank you for your candidacy",
    
    # Platform-specific (LinkedIn, Indeed, etc.)
    "application has been submitted",
    "your profile has been shared",
    "profile shared with",
    "application delivered",
    "your interest in",
    "your application was viewed",
    "application viewed by",
]

EXCLUSION_KEYWORDS = [
    # Strong rejection indicators
    "regret to inform",
    "unfortunately",
    "not selected",
    "not moving forward",
    "we are moving forward with other candidates",
    "decided not to move forward",
    "position has been filled",
    "no longer considering",
    "will not be moving forward",
    
    # Interview/assessment (but be more selective)
    "technical assessment",
    "coding challenge",
    "hackerrank",
    "codility",
    "codesignal",
    "phone screen scheduled",
    "interview scheduled",
    "interview invitation",
    
    # Marketing/newsletter (but allow job-related ones)
    "unsubscribe",
    "newsletter",
    "talent community",
    "talent network",
    "subscribe to",
    "email preferences",
    "marketing",
    
    # Background check/offer stage
    "background check",
    "offer letter",
    "employment offer",
    "salary negotiation",
    "start date",
    
    # General updates (be less restrictive)
    "survey",
    "feedback request",
]

JOB_ID_PATTERNS = [
    r"(?:job\s*id|requisition\s*id|req(?:uisition)?\s*#?|jr|r-)\s*[:#]?\s*([A-Za-z0-9\-_/]+)",
    r"\(?(?:id|job)\s*[:#]?\s*([A-Za-z0-9\-_/]+)\)?",
    r"\b([A-Z]{1,4}-\d{3,8})\b",  # patterns like R-12345, JR-123456, ABC-1234
    r"\b(\d{6,})\b",              # long numeric ids
]

ROLE_PATTERNS = [
    # Direct role mentions
    r"(?:for|position(?:\s*title)?|role(?:\s*title)?|applying\s*for)\s*[:\-]?\s*\"?([A-Za-z0-9 \-_/&()+.,']+)\"?",
    r"your application to .*? for ([A-Za-z0-9 \-_/&()+.,']+)",
    r"application received - ([A-Za-z0-9 \-_/&()+.,']+)",
    r"application for (?:the\s+)?(?:position\s+of\s+)?([A-Za-z0-9 \-_/&()+.,']+)",
    
    # Subject line patterns
    r"(?:intern|internship).*?(?:position|role).*?[:\-]\s*([A-Za-z0-9 \-_/&()+.,']+)",
    r"([A-Za-z0-9 \-_/&()+.,']*intern[A-Za-z0-9 \-_/&()+.,']*)\s*(?:position|role|application)",
    r"(?:summer|spring|fall|winter)\s+(?:20\d{2}\s+)?([A-Za-z0-9 \-_/&()+.,']*intern[A-Za-z0-9 \-_/&()+.,']*)",
    
    # Common formats
    r"job\s*title\s*[:\-]\s*([A-Za-z0-9 \-_/&()+.,']+)",
    r"role\s*[:\-]\s*([A-Za-z0-9 \-_/&()+.,']+)",
    r"position\s*[:\-]\s*([A-Za-z0-9 \-_/&()+.,']+)",
]

COMPANY_PATTERNS = [
    # Direct company mentions
    r"your application to ([A-Za-z0-9 .,&'()+\-_/]+)",
    r"application with ([A-Za-z0-9 .,&'()+\-_/]+)",
    r"application received (?:at|by)\s+([A-Za-z0-9 .,&'()+\-_/]+)",
    r"application (?:to|at|with)\s+([A-Za-z0-9 .,&'()+\-_/]+)",
    
    # Thank you messages
    r"thank you for (?:your )?(?:interest in|applying to|applying at)\s+([A-Za-z0-9 .,&'()+\-_/]+)",
    r"thanks for (?:your )?(?:interest in|applying to|applying at)\s+([A-Za-z0-9 .,&'()+\-_/]+)",
    
    # Confirmation patterns
    r"(?:we|team) (?:at|from)\s+([A-Za-z0-9 .,&'()+\-_/]+)\s+(?:received|confirm)",
    r"([A-Za-z0-9 .,&'()+\-_/]+)\s+(?:team|recruiting|talent|careers)",
    
    # From field fallback patterns (applied in extract_company function)
]

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def contains_confirmation(subject: str, body_snippet: str) -> bool:
    """
    More lenient confirmation detection:
    1. Check for confirmation keywords
    2. Only exclude if strong exclusion indicators are present
    3. Give more weight to subject line confirmations
    """
    subject_lower = (subject or "").lower()
    body_lower = (body_snippet or "").lower()
    text = f"{subject_lower}\n{body_lower}"
    
    # Check for confirmation keywords
    has_conf = any(k in text for k in CONFIRMATION_KEYWORDS)
    
    # Check for strong exclusion keywords (be more selective)
    strong_exclusions = [
        "regret to inform", "unfortunately", "not selected", "not moving forward",
        "position has been filled", "no longer considering", "unsubscribe",
        "newsletter", "background check", "offer letter", "employment offer"
    ]
    has_strong_excl = any(k in text for k in strong_exclusions)
    
    # If subject line has confirmation words, be more lenient
    subject_has_conf = any(k in subject_lower for k in [
        "application received", "application submitted", "thanks for applying",
        "thank you for applying", "application confirmation", "submission received"
    ])
    
    if subject_has_conf and not has_strong_excl:
        return True
    
    return has_conf and not has_strong_excl

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
