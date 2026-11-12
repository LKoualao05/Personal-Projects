"""In-memory stand-in for the Google Sheets v4 API (the subset the tracker uses)."""

from __future__ import annotations

import builtins
import re
from typing import Any


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_a1(rng: str) -> tuple[str, int, int, int | None, int | None]:
    """'Tab!B2:D' -> (tab, row0, col0, row1, col1), 1-based, None = open-ended."""
    tab, _, ref = rng.partition("!")
    a, _, b = ref.partition(":")
    b = b or a

    def cell(x: str) -> tuple[int | None, int | None]:
        m = re.fullmatch(r"([A-Z]*)(\d*)", x)
        assert m, x
        return (int(m[2]) if m[2] else None), (_col_index(m[1]) if m[1] else None)

    r0, c0 = cell(a)
    r1, c1 = cell(b)
    if ":" not in ref and r0 is not None and c0 is None:  # "Tab!1:1"-style handled above; "A2" single cell
        pass
    return tab, r0 or 1, c0 or 1, r1, c1


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries: int = 0):
        return self.fn()


class FakeSheetsService:
    def __init__(self, tabs: dict[str, list[list[str]]] | None = None, columns: int = 26):
        self.tabs = {name: [list(r) for r in rows] for name, rows in (tabs or {}).items()}
        self.columns = {name: columns for name in self.tabs}
        self.writes: list[tuple[str, list[list[Any]]]] = []  # (range, values) for assertions

    # spreadsheets() / values() chain -------------------------------------------
    def spreadsheets(self):
        return self

    def values(self):
        return _Values(self)

    def get(self, spreadsheetId: str):
        return _Exec(lambda: {"sheets": [
            {"properties": {"title": t, "sheetId": i, "gridProperties": {"columnCount": self.columns[t]}}}
            for i, t in enumerate(self.tabs)
        ]})

    def batchUpdate(self, spreadsheetId: str, body: dict):
        def run():
            for req in body["requests"]:
                if "addSheet" in req:
                    title = req["addSheet"]["properties"]["title"]
                    self.tabs[title], self.columns[title] = [], 26
                elif "appendDimension" in req:
                    title = list(self.tabs)[req["appendDimension"]["sheetId"]]
                    self.columns[title] += req["appendDimension"]["length"]
            return {}
        return _Exec(run)

    # grid helpers ----------------------------------------------------------------
    def _grid(self, tab: str) -> list[list[str]]:
        return self.tabs.setdefault(tab, [])

    def write(self, rng: str, values: list[list[Any]]) -> None:
        tab, r0, c0, _, _ = parse_a1(rng)
        grid = self._grid(tab)
        for dr, row in enumerate(values):
            for dc, v in enumerate(row):
                r, c = r0 + dr, c0 + dc
                if c > self.columns[tab]:
                    raise ValueError(f"Range {rng} exceeds grid limits ({self.columns[tab]} columns)")
                while len(grid) < r:
                    grid.append([])
                line = grid[r - 1]
                while len(line) < c:
                    line.append("")
                line[c - 1] = "" if v is None else str(v)
        self.writes.append((rng, values))

    def read(self, rng: str) -> list[list[str]]:
        tab, r0, c0, r1, c1 = parse_a1(rng)
        grid = self._grid(tab)
        out = []
        for r in range(r0, (r1 or len(grid)) + 1):
            line = grid[r - 1] if r <= len(grid) else []
            cells = line[c0 - 1: c1] if c1 else line[c0 - 1:]
            while cells and cells[-1] == "":
                cells = cells[:-1]
            out.append(cells)
        while out and not out[-1]:
            out.pop()
        return out


class _Values:
    def __init__(self, svc: FakeSheetsService):
        self.svc = svc

    def get(self, spreadsheetId: str, range: str):
        return _Exec(lambda: {"values": self.svc.read(range)})

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: dict):
        return _Exec(lambda: self.svc.write(range, body["values"]) or {})

    def batchUpdate(self, spreadsheetId: str, body: dict):
        def run():
            for d in body["data"]:
                self.svc.write(d["range"], d["values"])
            return {}
        return _Exec(run)

    def append(self, spreadsheetId: str, range: str, valueInputOption: str, insertDataOption: str, body: dict):
        def run():
            tab, _, c0, _, _ = parse_a1(range)
            grid = self.svc._grid(tab)
            last = max((i + 1 for i, line in enumerate(grid) if any(line)), default=0)
            letters = "".join(chr(64 + c0))
            self.svc.write(f"{tab}!{letters}{last + 1}", body["values"])
            return {}
        return _Exec(run)

    def clear(self, spreadsheetId: str, range: str):
        def run():
            tab, r0, c0, r1, c1 = parse_a1(range)
            grid = self.svc._grid(tab)
            for r in builtins.range(r0, (r1 or len(grid)) + 1):  # `range` is shadowed by the API kwarg
                if r <= len(grid):
                    line = grid[r - 1]
                    for c in builtins.range(c0, (c1 or len(line)) + 1):
                        if c <= len(line):
                            line[c - 1] = ""
            return {}
        return _Exec(run)
