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
from dataclasses import replace
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
    """Sheets-backed store that coexists with columns the user added by hand.

    Tracker columns are located by header name. Missing ones are appended to
    the right of the existing header, existing header cells are never renamed,
    and writes to existing rows only touch tracker-owned cells.
    """

    def __init__(self, sheets_service, settings: Settings):
        self.svc = sheets_service
        self.sid = settings.sheet_id
        self.app_tab = settings.sheet_name
        self.processed_tab = settings.processed_sheet_name
        self.summary_tab = settings.summary_sheet_name
        self._header: list[str] | None = None

    # -- helpers -----------------------------------------------------------
    def _values(self):
        return self.svc.spreadsheets().values()

    def _tab_props(self) -> dict[str, dict]:
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        return {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}

    def _read_header(self, title: str) -> list[str]:
        res = self._values().get(spreadsheetId=self.sid, range=f"{title}!1:1").execute()
        rows = res.get("values", [])
        return [str(h).strip() for h in rows[0]] if rows else []

    def _ensure_tab(self, title: str, headers: list[str], overwrite: bool = False) -> list[str]:
        """Create the tab if needed and make sure ``headers`` exist. Returns the final header.

        With ``overwrite`` (tracker-generated tabs only) the header row is replaced;
        otherwise missing headers are appended after the existing ones.
        """
        props = self._tab_props()
        if title not in props:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid, body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
            ).execute()
            props = self._tab_props()
        current = self._read_header(title)
        if overwrite:
            final = list(headers)
        else:
            while current and not current[-1]:
                current.pop()
            final = current + [h for h in headers if h not in current]
        if final != current:
            have = props[title].get("gridProperties", {}).get("columnCount", 26)
            if len(final) > have:
                self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={"requests": [{"appendDimension": {
                    "sheetId": props[title]["sheetId"], "dimension": "COLUMNS", "length": len(final) - have}}]}).execute()
            first_new = 0 if overwrite else len(current)
            self._values().update(
                spreadsheetId=self.sid, range=f"{title}!{_col(first_new + 1)}1", valueInputOption="RAW",
                body={"values": [final[first_new:]]},
            ).execute()
        return final

    def header(self) -> list[str]:
        if self._header is None:
            self._header = self._read_header(self.app_tab)
        return self._header

    def _owned_runs(self) -> list[tuple[int, int]]:
        """Contiguous (start, end) 0-based column index ranges owned by the tracker."""
        owned = [i for i, h in enumerate(self.header()) if h in APPLICATION_HEADERS]
        runs: list[tuple[int, int]] = []
        for i in owned:
            if runs and runs[-1][1] == i - 1:
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((i, i))
        return runs

    # -- TrackerStore ------------------------------------------------------
    def ensure_schema(self) -> None:
        self._header = self._ensure_tab(self.app_tab, APPLICATION_HEADERS)
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
        header = self.header() or list(APPLICATION_HEADERS)
        last = _col(max(len(header), 1))
        res = self._values().get(spreadsheetId=self.sid, range=f"{self.app_tab}!A2:{last}").execute()
        rows = []
        for i, vals in enumerate(res.get("values", []), start=2):
            if any(str(v).strip() for v in vals):
                rows.append(ApplicationRow.from_row(header, vals, row_number=i))
        return rows

    def _require_columns(self) -> list[str]:
        if not all(h in self.header() for h in APPLICATION_HEADERS):
            self.ensure_schema()
        return self.header()

    def append_applications(self, rows: list[ApplicationRow]) -> None:
        if not rows:
            return
        header = self._require_columns()
        self._values().append(
            spreadsheetId=self.sid, range=f"{self.app_tab}!A:{_col(len(header))}", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [r.to_row(header) for r in rows]},
        ).execute()

    def update_applications(self, rows: list[ApplicationRow]) -> None:
        rows = [r for r in rows if r.row_number]
        if not rows:
            return
        header = self._require_columns()
        data = []
        for r in rows:
            values = r.to_row(header)
            for start, end in self._owned_runs():  # never touch user-owned cells
                data.append({
                    "range": f"{self.app_tab}!{_col(start + 1)}{r.row_number}:{_col(end + 1)}{r.row_number}",
                    "values": [values[start:end + 1]],
                })
        self._values().batchUpdate(spreadsheetId=self.sid, body={"valueInputOption": "RAW", "data": data}).execute()

    def refresh_summary(self) -> None:
        table = summarize(self.list_applications())
        self._ensure_tab(self.summary_tab, [str(h) for h in table[0]], overwrite=True)
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
        return [replace(r, extra=dict(r.extra)) for r in self.rows]

    def append_applications(self, rows: list[ApplicationRow]) -> None:
        for r in rows:
            self._add(replace(r, extra=dict(r.extra)))

    def update_applications(self, rows: list[ApplicationRow]) -> None:
        for r in rows:
            # Like the Sheets store: tracker fields change, user columns are preserved.
            kept = self.rows[r.row_number - 2].extra
            self.rows[r.row_number - 2] = replace(r, extra=dict(kept))

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
