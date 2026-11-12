"""Shared data types: email messages, Gemini's structured output, sheet rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    STATUS_UPDATE = "status_update"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTION = "rejection"
    NOT_JOB_RELATED = "not_job_related"


# Status written to the sheet for each category.
STATUS_BY_CATEGORY: dict[Category, str] = {
    Category.APPLICATION_CONFIRMATION: "Applied",
    Category.STATUS_UPDATE: "In Review",
    Category.ASSESSMENT: "Assessment",
    Category.INTERVIEW: "Interview",
    Category.OFFER: "Offer",
    Category.REJECTION: "Rejected",
}
NEEDS_REVIEW_STATUS = "Needs Review"
STATUS_ORDER = ["Applied", "In Review", "Assessment", "Interview", "Offer", "Rejected", NEEDS_REVIEW_STATUS]


class EmailAnalysis(BaseModel):
    """Structured output Gemini must return for a single email."""

    category: Category = Field(description="Exactly one category for this email.")
    company: Optional[str] = Field(
        default=None,
        description="Hiring company. Never the ATS/job platform (Workday, Greenhouse, Lever, LinkedIn...) unless it is the employer.",
    )
    role_title: Optional[str] = Field(default=None, description="Role title exactly as written, without company or req id.")
    job_id: Optional[str] = Field(default=None, description="Requisition / job id exactly as written.")
    location: Optional[str] = Field(default=None, description="Job location as written.")
    is_internship: Optional[bool] = Field(default=None, description="True for internships/co-ops, false if clearly not, null if unclear.")
    summary: str = Field(description="One factual sentence, at most 30 words: what happened and any next step or deadline.")
    confidence: float = Field(description="Confidence in the category, 0 to 1.")


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    internal_date_ms: int
    subject: str
    sender: str
    snippet: str = ""
    body: str = ""
    links: list[str] = field(default_factory=list)

    def as_prompt_text(self, max_body_chars: int = 6000) -> str:
        body = self.body or self.snippet
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + " …[truncated]"
        return f"From: {self.sender}\nSubject: {self.subject}\n\nBody:\n<<<\n{body}\n>>>"


APPLICATION_HEADERS = [
    "Company",
    "Role Title",
    "Job ID",
    "Date Applied",
    "Gmail Thread URL",
    "From",
    "Subject",
    "Message ID",
    "Status",
    "Summary",
    "Location",
    "Internship",
    "Last Update",
    "Classified By",
]


@dataclass
class ApplicationRow:
    company: str = ""
    role_title: str = ""
    job_id: str = ""
    date_applied: str = ""
    thread_url: str = ""
    sender: str = ""
    subject: str = ""
    message_id: str = ""
    status: str = ""
    summary: str = ""
    location: str = ""
    internship: str = ""
    last_update: str = ""
    classified_by: str = ""
    # 1-based sheet row number; None for rows not yet written.
    row_number: Optional[int] = None

    def to_values(self) -> list[str]:
        return [
            self.company, self.role_title, self.job_id, self.date_applied, self.thread_url,
            self.sender, self.subject, self.message_id, self.status, self.summary,
            self.location, self.internship, self.last_update, self.classified_by,
        ]

    @classmethod
    def from_values(cls, values: list[str], row_number: Optional[int] = None) -> "ApplicationRow":
        padded = list(values) + [""] * (len(APPLICATION_HEADERS) - len(values))
        return cls(*[str(v) for v in padded[: len(APPLICATION_HEADERS)]], row_number=row_number)
