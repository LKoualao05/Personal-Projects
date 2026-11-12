"""Test doubles for Gmail, Gemini and classifiers. No network access."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from tracker.llm import CallStats, Classification
from tracker.schemas import Category, EmailAnalysis, EmailMessage

DAY_MS = 86_400_000
T0 = 1_760_000_000_000  # 2025-10-09


def gmail_message(mid: str, subject: str, body: str, *, sender: str = "Careers <jobs@example.com>",
                  day: int = 0, html: bool = False, thread_id: str | None = None) -> dict[str, Any]:
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": mid,
        "threadId": thread_id or f"thread-{mid}",
        "internalDate": str(T0 + day * DAY_MS),
        "snippet": body[:100],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": sender}],
            "parts": [{"mimeType": "text/html" if html else "text/plain", "body": {"data": data}}],
        },
    }


class _Exec:
    def __init__(self, value: Any):
        self.value = value

    def execute(self) -> Any:
        return self.value


class FakeGmail:
    """Implements the slice of the Gmail API the tracker uses."""

    def __init__(self, messages: list[dict[str, Any]]):
        self._msgs = {m["id"]: m for m in messages}
        self.queries: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId: str, q: str, pageToken=None, maxResults: int = 500):
        self.queries.append(q)
        return _Exec({"messages": [{"id": mid} for mid in self._msgs][:maxResults]})

    def get(self, userId: str, id: str, format: str = "full", metadataHeaders=None):
        return _Exec(self._msgs[id])


def analysis(category: Category, company: str | None = "Acme", role: str | None = "Software Engineering Intern",
             **kw: Any) -> EmailAnalysis:
    return EmailAnalysis(category=category, company=company, role_title=role,
                         summary=kw.pop("summary", f"{category.value} from {company}"),
                         confidence=kw.pop("confidence", 0.9), **kw)


class FakeClassifier:
    """Returns a pre-set analysis per message id; ids in ``fail`` raise."""

    def __init__(self, results: dict[str, EmailAnalysis], fail: set[str] | None = None, name: str = "fake-gemini"):
        self.results = results
        self.fail = fail or set()
        self.name = name
        self.seen: list[str] = []

    def classify(self, email: EmailMessage) -> Classification:
        self.seen.append(email.id)
        if email.id in self.fail:
            raise RuntimeError("boom")
        return Classification(self.results[email.id], self.name, CallStats(self.name, 0.1, 100, 20))


class FakeGenaiClient:
    """Mimics ``genai.Client().models.generate_content``."""

    def __init__(self, parsed: Any = None, text: str = "", usage: Any = None):
        self.calls: list[dict[str, Any]] = []
        self._resp = SimpleNamespace(
            parsed=parsed,
            text=text,
            usage_metadata=usage or SimpleNamespace(
                prompt_token_count=420, candidates_token_count=60, thoughts_token_count=12, cached_content_token_count=0
            ),
        )
        self.models = self

    def generate_content(self, *, model: str, contents: Any, config: Any = None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._resp
