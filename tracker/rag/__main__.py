"""Job-description RAG CLI.

    python -m tracker.rag add --company Acme --role "SWE Intern" --url https://boards.greenhouse.io/acme/jobs/123
    python -m tracker.rag add --company Acme --role "SWE Intern" --file jd.txt
    python -m tracker.rag search "computer vision with PyTorch" -k 5
    python -m tracker.rag ask "Which of my roles mention Kubernetes, and what level is expected?"
    python -m tracker.rag backfill            # index JDs for applications already in the sheet
    python -m tracker.rag stats
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

from ..config import load_settings
from ..schemas import Category, EmailAnalysis
from .index import make_jd_index


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tracker.rag", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="index one job description")
    add.add_argument("--company", required=True)
    add.add_argument("--role", default="")
    add.add_argument("--job-id", default="")
    src = add.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--file")
    src.add_argument("--text")
    for name in ("search", "ask"):
        q = sub.add_parser(name)
        q.add_argument("query")
        q.add_argument("-k", type=int, default=5 if name == "search" else 6)
        q.add_argument("--company", default="")
    bf = sub.add_parser("backfill", help="index JDs for rows already in the tracker sheet")
    bf.add_argument("--limit", type=int, default=None)
    bf.add_argument("--force", action="store_true", help="re-index jobs that are already indexed")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    settings = load_settings()
    index = make_jd_index(settings)
    if index is None:
        return 1

    if args.cmd == "add":
        if args.url:
            n = index.add_from_url(args.company, args.role, args.url, args.job_id)
        else:
            text = Path(args.file).read_text() if args.file else args.text
            n = index.add(args.company, args.role, text, job_id=args.job_id)
        print(f"Indexed {n} chunks for {args.company} — {args.role}")
    elif args.cmd == "search":
        for i, h in enumerate(index.search(args.query, k=args.k, company=args.company), start=1):
            c = h.chunk
            print(f"[{i}] {h.score:.3f}  {c.company} — {c.role_title}  ({c.source_url or c.source})")
            print(textwrap.indent(textwrap.shorten(c.text, 300), "     "))
    elif args.cmd == "ask":
        ans = index.answer(args.query, k=args.k, company=args.company)
        print(ans.answer, "\n")
        for s in ans.sources:
            print(f"  [{s['n']}] {s['company']} — {s['role_title']}  {s['source_url']}")
    elif args.cmd == "backfill":
        from ..auth import build_services, load_user_credentials
        from ..gmail_client import fetch_message
        from ..sheets_io import GoogleSheetsStore

        gmail, sheets = build_services(load_user_credentials(settings, interactive=sys.stdin.isatty()))
        rows = [r for r in GoogleSheetsStore(sheets, settings).list_applications() if r.message_id and r.company]
        rows = rows[: args.limit] if args.limit else rows
        total = jobs = 0
        for r in rows:
            try:
                email = fetch_message(gmail, r.message_id)
                a = EmailAnalysis(category=Category.APPLICATION_CONFIRMATION, company=r.company,
                                  role_title=r.role_title or None, job_id=r.job_id or None, summary=r.summary, confidence=1.0)
                n = index.ingest_email(email, a, force=args.force)
            except Exception as exc:
                print(f"  ! {r.company}: {exc}")
                continue
            total += n
            jobs += bool(n)
            if n:
                print(f"  + {r.company} — {r.role_title}: {n} chunks")
        print(f"Indexed {jobs} job descriptions ({total} chunks) from {len(rows)} applications")
    elif args.cmd == "stats":
        print(f"{index.store.count()} chunks indexed ({settings.vector_store}, {index.embedder.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
