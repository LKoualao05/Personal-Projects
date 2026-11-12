"""Google Sheets storage for tracked applications.

Tabs:
- ``Applications``: one row per application (see ``APPLICATION_HEADERS``).
- ``ProcessedMessageIds``: every Gmail message already classified, so it is
  never re-processed (or re-billed).
- ``CompanySummary``: per-company counts by status, rewritten on each sync.
"""

from __future__ import annotations

import string
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable, Protocol

from .config import Settings
from .schemas import APPLICATION_HEADERS, STATUS_ORDER, ApplicationRow

PROCESSED_HEADERS = ["Message ID", "Category", "Processed At"]


class TrackerStore(Protocol):
    def ensure_schema(self) -> None: ...
    def processed_ids(self) -> set[str]: ...
    def mark_processed(self, items: Iterable[tuple[str, str]]) -> None: ...
    def list_applications(self) -> list[ApplicationRow]: ...
    def append_applications(self, rows: list[ApplicationRow]) -> None: ...
    def update_applications(self, rows: list[ApplicationRow]) -> None: ...
    def refresh_summary(self) -> None: ...


def _col(n: int) -> str:
    """1-based column index -> A1 letter (supports up to ZZ)."""
    letters = string.ascii_uppercase
    return letters[n - 1] if n <= 26 else letters[(n - 1) // 26 - 1] + letters[(n - 1) % 26]


LAST_COL = _col(len(APPLICATION_HEADERS))


def summarize(rows: list[ApplicationRow]) -> list[list[str | int]]:
    """Company summary table: company, total, one column per status, latest update."""
    by_company: dict[str, Counter] = defaultdict(Counter)
    latest: dict[str, str] = {}
    for r in rows:
        if not r.company:
            continue
        by_company[r.company][r.status or "Applied"] += 1
        stamp = r.last_update or r.date_applied
        latest[r.company] = max(latest.get(r.company, ""), stamp)
    header: list[str | int] = ["Company", "Applications", *STATUS_ORDER, "Latest Activity"]
    table: list[list[str | int]] = [header]
    for company in sorted(by_company, key=lambda c: (-sum(by_company[c].values()), c.lower())):
        counts = by_company[company]
        table.append([company, sum(counts.values()), *[counts.get(s, 0) for s in STATUS_ORDER], latest.get(company, "")])
    return table


class GoogleSheetsStore:
    def __init__(self, sheets_service, settings: Settings):
        self.svc = sheets_service
        self.sid = settings.sheet_id
        self.app_tab = settings.sheet_name
        self.processed_tab = settings.processed_sheet_name
        self.summary_tab = settings.summary_sheet_name

    # -- helpers -----------------------------------------------------------
    def _values(self):
        return self.svc.spreadsheets().values()

    def _ensure_tab(self, title: str, headers: list[str]) -> None:
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        titles = {s["properties"]["title"] for s in meta["sheets"]}
        if title not in titles:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid, body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
            ).execute()
        rng = f"{title}!1:1"
        row = self._values().get(spreadsheetId=self.sid, range=rng).execute().get("values", [])
        if not row or row[0] != headers:
            self._values().update(
                spreadsheetId=self.sid, range=rng, valueInputOption="RAW", body={"values": [headers]}
            ).execute()

    # -- TrackerStore ------------------------------------------------------
    def ensure_schema(self) -> None:
        self._ensure_tab(self.app_tab, APPLICATION_HEADERS)
        self._ensure_tab(self.processed_tab, PROCESSED_HEADERS)

    def processed_ids(self) -> set[str]:
        res = self._values().get(spreadsheetId=self.sid, range=f"{self.processed_tab}!A2:A").execute()
        return {v[0] for v in res.get("values", []) if v}

    def mark_processed(self, items: Iterable[tuple[str, str]]) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        values = [[mid, cat, now] for mid, cat in items]
        if values:
            self._values().append(
                spreadsheetId=self.sid, range=f"{self.processed_tab}!A:C", valueInputOption="RAW",
                insertDataOption="INSERT_ROWS", body={"values": values},
            ).execute()

    def list_applications(self) -> list[ApplicationRow]:
        res = self._values().get(spreadsheetId=self.sid, range=f"{self.app_tab}!A2:{LAST_COL}").execute()
        rows = []
        for i, vals in enumerate(res.get("values", []), start=2):
            if any(str(v).strip() for v in vals):
                rows.append(ApplicationRow.from_values(vals, row_number=i))
        return rows

    def append_applications(self, rows: list[ApplicationRow]) -> None:
        if rows:
            self._values().append(
                spreadsheetId=self.sid, range=f"{self.app_tab}!A:{LAST_COL}", valueInputOption="RAW",
                insertDataOption="INSERT_ROWS", body={"values": [r.to_values() for r in rows]},
            ).execute()

    def update_applications(self, rows: list[ApplicationRow]) -> None:
        data = [
            {"range": f"{self.app_tab}!A{r.row_number}:{LAST_COL}{r.row_number}", "values": [r.to_values()]}
            for r in rows if r.row_number
        ]
        if data:
            self._values().batchUpdate(
                spreadsheetId=self.sid, body={"valueInputOption": "RAW", "data": data}
            ).execute()

    def refresh_summary(self) -> None:
        table = summarize(self.list_applications())
        self._ensure_tab(self.summary_tab, [str(h) for h in table[0]])
        self._values().clear(spreadsheetId=self.sid, range=f"{self.summary_tab}!A2:Z").execute()
        if len(table) > 1:
            self._values().update(
                spreadsheetId=self.sid, range=f"{self.summary_tab}!A2", valueInputOption="RAW",
                body={"values": table[1:]},
            ).execute()


class InMemoryStore:
    """Store used by tests and the eval harness."""

    def __init__(self, rows: list[ApplicationRow] | None = None, processed: set[str] | None = None):
        self.rows: list[ApplicationRow] = []
        self.processed: dict[str, str] = {m: "" for m in (processed or set())}
        self.summary: list[list[str | int]] = []
        for r in rows or []:
            self._add(r)

    def _add(self, row: ApplicationRow) -> None:
        row.row_number = len(self.rows) + 2
        self.rows.append(row)

    def ensure_schema(self) -> None:
        pass

    def processed_ids(self) -> set[str]:
        return set(self.processed)

    def mark_processed(self, items: Iterable[tuple[str, str]]) -> None:
        for mid, cat in items:
            self.processed[mid] = cat

    def list_applications(self) -> list[ApplicationRow]:
        return [ApplicationRow.from_values(r.to_values(), r.row_number) for r in self.rows]

    def append_applications(self, rows: list[ApplicationRow]) -> None:
        for r in rows:
            self._add(ApplicationRow.from_values(r.to_values()))

    def update_applications(self, rows: list[ApplicationRow]) -> None:
        for r in rows:
            self.rows[r.row_number - 2] = ApplicationRow.from_values(r.to_values(), r.row_number)

    def refresh_summary(self) -> None:
        self.summary = summarize(self.rows)


class DryRunStore:
    """Reads from a real store, records writes instead of performing them."""

    def __init__(self, base: TrackerStore):
        self.base = base
        self.appended: list[ApplicationRow] = []
        self.updated: list[ApplicationRow] = []
        self.marked: list[tuple[str, str]] = []

    def ensure_schema(self) -> None:
        pass

    def processed_ids(self) -> set[str]:
        return self.base.processed_ids()

    def list_applications(self) -> list[ApplicationRow]:
        return self.base.list_applications()

    def mark_processed(self, items: Iterable[tuple[str, str]]) -> None:
        self.marked.extend(items)

    def append_applications(self, rows: list[ApplicationRow]) -> None:
        self.appended.extend(rows)

    def update_applications(self, rows: list[ApplicationRow]) -> None:
        self.updated.extend(rows)

    def refresh_summary(self) -> None:
        pass
