"""Find job-posting links in emails and fetch the posting text.

Only links that match known job-board URL patterns are fetched. Tracking and
redirect links in emails can carry personal tokens, so they are never followed.
Greenhouse, Lever, Workday and LinkedIn postings are read from their public
JSON / guest endpoints; anything else falls back to plain HTML extraction.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Any, Optional

# Generic on purpose: never send identifying details to third-party job boards.
USER_AGENT = "Mozilla/5.0 (compatible; job-description-fetcher/1.0)"

GREENHOUSE = re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?P<board>[\w-]+)/jobs/(?P<id>\d+)")
LEVER = re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/(?P<company>[\w.-]+)/(?P<id>[0-9a-f]{8}-[0-9a-f-]{27})")
WORKDAY = re.compile(
    r"https?://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)/job/(?P<path>[^?#\s]+)"
)
LINKEDIN = re.compile(r"https?://(?:[\w]+\.)?linkedin\.com/(?:comm/)?jobs/view/(?:[\w-]*?-)?(?P<id>\d{6,})")
OTHER_BOARDS = [
    re.compile(r"https?://jobs\.ashbyhq\.com/[\w.-]+/[0-9a-f-]{36}"),
    re.compile(r"https?://jobs\.smartrecruiters\.com/[\w.-]+/\d+"),
    re.compile(r"https?://[\w.-]+\.icims\.com/jobs/\d+"),
    re.compile(r"https?://[\w.-]+\.(?:joinhandshake|handshake)\.com/(?:stu/)?jobs/\d+"),
    re.compile(r"https?://[\w.-]+\.bamboohr\.com/careers/\d+"),
]
ALL_PATTERNS = [GREENHOUSE, LEVER, WORKDAY, LINKEDIN, *OTHER_BOARDS]

JD_KEYWORDS = [
    "responsibilities", "qualifications", "requirements", "what you'll do", "what you will do",
    "about the role", "preferred", "basic qualifications", "minimum qualifications", "you will",
    "what we're looking for", "about you", "skills",
]


@dataclass
class FetchedPosting:
    url: str
    title: str
    text: str


def find_job_posting_urls(links: list[str]) -> list[str]:
    out: dict[str, None] = {}
    for link in links:
        for pat in ALL_PATTERNS:
            m = pat.search(link)
            if m:
                out.setdefault(m.group(0), None)
                break
    return list(out)


def looks_like_job_description(text: str) -> bool:
    lower = (text or "").lower()
    return len(lower) >= 800 and sum(k in lower for k in JD_KEYWORDS) >= 2


def endpoint_for(url: str) -> tuple[str, str]:
    """(fetch_url, kind) for a posting URL."""
    if m := GREENHOUSE.search(url):
        return f"https://boards-api.greenhouse.io/v1/boards/{m['board']}/jobs/{m['id']}", "greenhouse"
    if m := LEVER.search(url):
        return f"https://api.lever.co/v0/postings/{m['company']}/{m['id']}", "lever"
    if m := WORKDAY.search(url):
        host = f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com"
        return f"{host}/wday/cxs/{m['tenant']}/{m['site']}/job/{m['path']}", "workday"
    if m := LINKEDIN.search(url):
        return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m['id']}", "linkedin"
    return url, "html"


def _html_text(markup: str) -> str:
    from ..gmail_client import html_to_text, normalize_body

    return normalize_body(html_to_text(markup))


def parse_posting(kind: str, payload: Any) -> tuple[str, str]:
    """(title, text) from an endpoint response (JSON dict or HTML string)."""
    if kind == "greenhouse":
        return payload.get("title", ""), _html_text(html_lib.unescape(payload.get("content", "")))
    if kind == "lever":
        parts = [payload.get("descriptionPlain", "")]
        for section in payload.get("lists", []):
            parts.append(f"{section.get('text', '')}\n{_html_text(section.get('content', ''))}")
        parts.append(payload.get("additionalPlain", ""))
        return payload.get("text", ""), "\n\n".join(p for p in parts if p).strip()
    if kind == "workday":
        info = payload.get("jobPostingInfo", {})
        return info.get("title", ""), _html_text(info.get("jobDescription", ""))
    if kind == "linkedin":
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(payload, "html.parser")
        title = soup.find(["h1", "h2"])
        desc = soup.select_one(".show-more-less-html__markup, .description__text") or soup
        return (title.get_text(" ", strip=True) if title else ""), _html_text(str(desc))
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(payload, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return title, _html_text(str(main))


def fetch_posting(url: str, http: Any = None, timeout: float = 15.0) -> Optional[FetchedPosting]:
    """Fetch and parse one posting. Returns None if unreachable or too short."""
    import httpx

    fetch_url, kind = endpoint_for(url)
    client = http or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.get(fetch_url, headers={"Accept": "application/json" if kind in {"greenhouse", "lever", "workday"} else "text/html"})
        if resp.status_code != 200:
            return None
        payload = resp.json() if kind in {"greenhouse", "lever", "workday"} else resp.text
        title, text = parse_posting(kind, payload)
    except Exception:
        return None
    finally:
        if http is None:
            client.close()
    return FetchedPosting(url=url, title=title, text=text) if len(text) >= 300 else None
