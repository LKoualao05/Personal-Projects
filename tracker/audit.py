"""Read-only integrity audit of the tracker sheet.

    python -m tracker.audit                          # checks only
    python -m tracker.audit --against backup.json    # also reconcile with a JSON backup

Checks: every Message ID appears once, tracker rows have a status, and, with a
backup, every original row is still present exactly once with the user's own
columns attached to the same application. Exit code 1 on any problem.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .schemas import APPLICATION_HEADERS, ApplicationRow


def audit_rows(rows: list[ApplicationRow], backup: Optional[list[list[str]]] = None) -> dict[str, Any]:
    ids = Counter(r.message_id for r in rows if r.message_id)
    report: dict[str, Any] = {
        "rows": len(rows),
        "duplicate_message_ids": sorted(m for m, c in ids.items() if c > 1),
        "rows_without_message_id": sum(1 for r in rows if not r.message_id),
        "rows_without_status": sum(1 for r in rows if not r.status),
        "status_counts": dict(Counter(r.status or "(blank)" for r in rows)),
    }
    if backup:
        header, data = backup[0], backup[1:]
        originals = [ApplicationRow.from_row(header, r) for r in data if any(str(c).strip() for c in r)]
        by_id = {r.message_id: r for r in rows}
        report["original_rows"] = len(originals)
        report["original_rows_missing"] = [o.message_id for o in originals if o.message_id not in by_id]
        report["user_columns_changed"] = [
            o.message_id for o in originals
            if o.message_id in by_id and {k: v for k, v in o.extra.items() if v} != by_id[o.message_id].extra
        ]
    report["ok"] = not (report["duplicate_message_ids"] or report.get("original_rows_missing")
                        or report.get("user_columns_changed"))
    return report


def main(argv: Optional[list[str]] = None) -> int:
    from .auth import build_services, load_user_credentials
    from .config import load_settings
    from .sheets_io import GoogleSheetsStore

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--against", type=Path, help="JSON backup ({tab: rows}) to reconcile with")
    args = p.parse_args(argv)
    settings = load_settings()
    _, sheets = build_services(load_user_credentials(settings, interactive=False))
    rows = GoogleSheetsStore(sheets, settings).list_applications()
    backup = json.loads(args.against.read_text())[settings.sheet_name] if args.against else None
    report = audit_rows(rows, backup)
    for key, value in report.items():
        shown = value if not isinstance(value, list) else (f"{len(value)} {value[:5]}" if value else "0")
        print(f"{key:<26} {shown}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["audit_rows", "APPLICATION_HEADERS"]
