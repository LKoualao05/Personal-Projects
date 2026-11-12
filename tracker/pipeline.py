"""Daily sync: Gmail -> classify (Gemini or heuristics) -> Google Sheets.

1. Search Gmail for application-related emails not yet processed.
2. Fetch and classify each one (in parallel for Gemini).
3. Apply results in chronological order: confirmations create rows, later
   emails (assessment, interview, offer, rejection...) update the status of
   the matching application.
4. Record every classified message id so nothing is re-processed or re-billed.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import Settings
from .gmail_client import build_thread_url, fetch_message, iso_date_from_internal, search_candidate_messages
from .llm import CallStats, Classification, EmailClassifier
from .schemas import (
    NEEDS_REVIEW_STATUS,
    STATUS_BY_CATEGORY,
    ApplicationRow,
    Category,
    EmailAnalysis,
    EmailMessage,
)
from .sheets_io import TrackerStore

# Optional hook called with each job-related (email, classification), skipped in
# dry runs. Used by RAG ingestion; an int return value counts indexed chunks.
OnClassified = Callable[[EmailMessage, Classification], Optional[int]]

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|the|group|holdings|careers|recruiting|university recruiting)\b"
)


def normalize_company(name: Optional[str]) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _COMPANY_SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: Optional[str]) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in {"the", "a", "of", "and", "for", "to"}}


def role_similarity(a: Optional[str], b: Optional[str]) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_matching_application(rows: list[ApplicationRow], analysis: EmailAnalysis) -> Optional[ApplicationRow]:
    """Best existing row for the company/role/job id in ``analysis``."""
    company = normalize_company(analysis.company)
    if not company:
        return None
    candidates = [r for r in rows if normalize_company(r.company) == company]
    if not candidates:
        return None
    if analysis.job_id:
        for r in candidates:
            if r.job_id and r.job_id.lower() == analysis.job_id.lower():
                return r
    if analysis.role_title:
        best = max(candidates, key=lambda r: role_similarity(r.role_title, analysis.role_title))
        if role_similarity(best.role_title, analysis.role_title) >= 0.5:
            return best
    if len(candidates) == 1:
        return candidates[0]
    # Several applications at this company and no role info: most recent one.
    return max(candidates, key=lambda r: r.last_update or r.date_applied)


def is_duplicate_confirmation(row: ApplicationRow, analysis: EmailAnalysis) -> bool:
    if analysis.job_id and row.job_id:
        return analysis.job_id.lower() == row.job_id.lower()
    if analysis.role_title and row.role_title:
        return role_similarity(row.role_title, analysis.role_title) >= 0.8
    return not analysis.role_title and not row.role_title


@dataclass
class SyncResult:
    candidates: int = 0
    already_processed: int = 0
    classified: int = 0
    new_applications: int = 0
    status_updates: int = 0
    duplicates: int = 0
    ignored: int = 0
    backfilled: int = 0
    jd_chunks_indexed: int = 0
    errors: list[str] = field(default_factory=list)
    stats: list[CallStats] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    classifier: str = ""
    dry_run: bool = False

    def report(self) -> str:
        tokens_in = sum(s.input_tokens for s in self.stats)
        tokens_out = sum(s.output_tokens + s.thinking_tokens for s in self.stats)
        avg_latency = sum(s.latency_s for s in self.stats) / len(self.stats) if self.stats else 0.0
        lines = [
            f"{'[DRY RUN] ' if self.dry_run else ''}Sync complete (classifier: {self.classifier})",
            f"  candidate emails:    {self.candidates} ({self.already_processed} already processed)",
            f"  classified:          {self.classified}",
            f"  new applications:    {self.new_applications}",
            f"  status updates:      {self.status_updates}",
            f"  duplicates skipped:  {self.duplicates}",
            f"  not job-related:     {self.ignored}",
            f"  backfilled rows:     {self.backfilled}",
            f"  JD chunks indexed:   {self.jd_chunks_indexed}",
            f"  errors:              {len(self.errors)}",
        ]
        if self.stats:
            lines.append(f"  LLM tokens in/out:   {tokens_in:,} / {tokens_out:,}  (avg latency {avg_latency:.2f}s)")
        return "\n".join(lines)


def _row_from_email(email: EmailMessage, c: Classification, tz_name: str, status: str) -> ApplicationRow:
    a = c.analysis
    date = iso_date_from_internal(email.internal_date_ms, tz_name)
    return ApplicationRow(
        company=a.company or "",
        role_title=a.role_title or "",
        job_id=a.job_id or "",
        date_applied=date if a.category == Category.APPLICATION_CONFIRMATION else "",
        thread_url=build_thread_url(email.thread_id),
        sender=email.sender,
        subject=email.subject,
        message_id=email.id,
        status=status,
        summary=a.summary,
        location=a.location or "",
        internship=_yes_no(a.is_internship),
        last_update=date,
        classified_by=c.classified_by,
    )


def _yes_no(v: Optional[bool]) -> str:
    return "" if v is None else ("Yes" if v else "No")


def _fill_blanks(row: ApplicationRow, a: EmailAnalysis) -> None:
    row.company = row.company or (a.company or "")
    row.role_title = row.role_title or (a.role_title or "")
    row.job_id = row.job_id or (a.job_id or "")
    row.location = row.location or (a.location or "")
    row.internship = row.internship or _yes_no(a.is_internship)


def classify_all(
    classifier: EmailClassifier, emails: list[EmailMessage], max_workers: int, log=print
) -> list[tuple[EmailMessage, Optional[Classification], Optional[str]]]:
    def one(e: EmailMessage):
        try:
            return e, classifier.classify(e), None
        except Exception as exc:
            return e, None, f"{e.id}: {type(exc).__name__}: {exc}"

    workers = max(1, max_workers if classifier.name != "heuristic" else 1)
    out = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, res in enumerate(pool.map(one, emails), start=1):
            out.append(res)
            if i % 25 == 0:
                log(f"  classified {i}/{len(emails)}")
    return out


def run_sync(
    settings: Settings,
    gmail,
    store: TrackerStore,
    classifier: EmailClassifier,
    *,
    limit: Optional[int] = None,
    backfill: bool = False,
    dry_run: bool = False,
    on_classified: Optional[OnClassified] = None,
    log=print,
) -> SyncResult:
    result = SyncResult(classifier=classifier.name, dry_run=dry_run)
    store.ensure_schema()
    processed = store.processed_ids()
    rows = store.list_applications()

    log("Searching Gmail for application emails...")
    ids = search_candidate_messages(gmail, settings.gmail_search_after, log=log)
    result.candidates = len(ids)
    new_ids = [i for i in ids if i not in processed]
    result.already_processed = len(ids) - len(new_ids)
    if limit is not None:
        new_ids = new_ids[:limit]
    log(f"Found {len(ids)} candidates, {len(new_ids)} to classify")

    emails: list[EmailMessage] = []
    for mid in new_ids:
        try:
            emails.append(fetch_message(gmail, mid))
        except Exception as exc:
            result.errors.append(f"{mid}: fetch failed: {exc}")
    emails.sort(key=lambda e: e.internal_date_ms)  # status changes apply in order

    new_rows: list[ApplicationRow] = []
    dirty: dict[int, ApplicationRow] = {}
    processed_items: list[tuple[str, str]] = []

    for email, c, err in classify_all(classifier, emails, settings.llm_max_workers, log):
        if err or c is None:
            result.errors.append(err or f"{email.id}: no result")
            continue  # not marked processed: retried next run
        result.classified += 1
        if c.stats and c.stats.input_tokens:
            result.stats.append(c.stats)
        processed_items.append((email.id, c.analysis.category.value))

        a = c.analysis
        if a.category == Category.NOT_JOB_RELATED:
            result.ignored += 1
            continue

        if on_classified and not dry_run:
            try:
                indexed = on_classified(email, c)
                result.jd_chunks_indexed += indexed if isinstance(indexed, int) else 0
            except Exception as exc:
                result.errors.append(f"{email.id}: on_classified hook failed: {exc}")

        status = STATUS_BY_CATEGORY[a.category]
        date = iso_date_from_internal(email.internal_date_ms, settings.timezone)
        match = find_matching_application(rows + new_rows, a)

        if a.category == Category.APPLICATION_CONFIRMATION:
            if match and is_duplicate_confirmation(match, a):
                result.duplicates += 1
                continue
            row = _row_from_email(email, c, settings.timezone, status)
            new_rows.append(row)
            result.new_applications += 1
            result.changes.append(f"+ {row.company or '?'} — {row.role_title or '?'} [{status}]")
            continue

        if match is None:
            row = _row_from_email(email, c, settings.timezone, status)
            new_rows.append(row)
            result.new_applications += 1
            result.changes.append(f"+ {row.company or '?'} — {row.role_title or '?'} [{status}] (no confirmation found)")
            continue

        old = match.status or "Applied"
        _fill_blanks(match, a)
        if match.row_number:
            dirty[match.row_number] = match
        if date >= (match.last_update or match.date_applied or ""):
            match.status = status
            match.summary = a.summary
            match.last_update = date
            match.classified_by = c.classified_by
            result.status_updates += 1
            result.changes.append(f"~ {match.company} — {match.role_title or '?'}: {old} -> {status}")
        else:
            result.changes.append(f"  {match.company}: older '{status}' email ignored (kept {old})")

    if backfill:
        _backfill(settings, gmail, rows, dirty, classifier, result, limit, log)

    if dry_run:
        log("Dry run: no changes written.")
    store.append_applications(new_rows)
    store.update_applications(list(dirty.values()))
    store.mark_processed(processed_items)
    if new_rows or dirty:
        store.refresh_summary()
    return result


def _backfill(settings, gmail, rows, dirty, classifier, result, limit, log) -> None:
    """Classify existing rows (from the old regex tracker) that have no summary."""
    todo = [r for r in rows if not r.summary and r.message_id and r.row_number not in dirty]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        return
    log(f"Backfilling {len(todo)} existing rows...")
    emails, by_id = [], {}
    for r in todo:
        try:
            e = fetch_message(gmail, r.message_id)
            emails.append(e)
            by_id[e.id] = r
        except Exception as exc:
            result.errors.append(f"{r.message_id}: backfill fetch failed: {exc}")
    for email, c, err in classify_all(classifier, emails, settings.llm_max_workers, log):
        if err or c is None:
            result.errors.append(err or f"{email.id}: no result")
            continue
        if c.stats and c.stats.input_tokens:
            result.stats.append(c.stats)
        row, a = by_id[email.id], c.analysis
        row.summary = a.summary
        row.classified_by = c.classified_by
        row.last_update = row.last_update or row.date_applied
        if a.category == Category.NOT_JOB_RELATED:
            row.status = row.status or NEEDS_REVIEW_STATUS
        else:
            row.status = row.status or STATUS_BY_CATEGORY[a.category]
            _fill_blanks(row, a)
        dirty[row.row_number] = row
        result.backfilled += 1
