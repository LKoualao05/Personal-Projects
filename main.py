#!/usr/bin/env python3
"""Sync Gmail application emails into the Google Sheet.

Examples:
    python main.py                 # classify new emails with Gemini, update the sheet
    python main.py --dry-run       # show what would change, write nothing
    python main.py --backfill      # also summarize rows created by the old regex tracker
    python main.py --no-llm        # heuristic classifier only (no Vertex AI needed)
    python main.py --no-rag        # don't index job descriptions this run
    python main.py --reauth        # redo the Gmail/Sheets OAuth consent in a browser
"""

from __future__ import annotations

import argparse
import sys

from tracker.auth import build_services, load_user_credentials, run_consent_flow
from tracker.config import load_settings
from tracker.llm import make_classifier
from tracker.pipeline import run_sync
from tracker.rag import make_jd_index
from tracker.sheets_io import DryRunStore, GoogleSheetsStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="classify but do not write to the sheet")
    p.add_argument("--no-llm", action="store_true", help="use the keyword/regex classifier instead of Gemini")
    p.add_argument("--backfill", action="store_true", help="classify existing rows that have no summary yet")
    p.add_argument("--limit", type=int, default=None, help="max emails (and backfill rows) to classify this run")
    p.add_argument("--model", default=None, help="override GEMINI_MODEL for this run")
    p.add_argument("--no-rag", action="store_true", help="skip indexing job descriptions")
    p.add_argument("--reauth", action="store_true", help="run the OAuth consent flow and save token.json")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    overrides = {}
    if args.no_llm:
        overrides["use_llm"] = False
    if args.model:
        overrides["gemini_model"] = args.model
    if args.no_rag:
        overrides["rag_enabled"] = False
    settings = load_settings(**overrides)

    if args.reauth:
        run_consent_flow(settings)
        print(f"Saved new OAuth token to {settings.resolve(settings.token_path)}")
        return 0

    creds = load_user_credentials(settings, interactive=sys.stdin.isatty())
    gmail, sheets = build_services(creds)
    store = GoogleSheetsStore(sheets, settings)
    if args.dry_run:
        store = DryRunStore(store)

    jd_index = make_jd_index(settings)
    on_classified = (lambda email, c: jd_index.ingest_email(email, c.analysis)) if jd_index else None

    result = run_sync(
        settings, gmail, store, make_classifier(settings),
        limit=args.limit, backfill=args.backfill, dry_run=args.dry_run, on_classified=on_classified,
    )
    print()
    for change in result.changes:
        print(change)
    print(result.report())
    for err in result.errors[:10]:
        print(f"  ! {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
