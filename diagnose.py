#!/usr/bin/env python3
"""Show how candidate emails are classified, heuristic vs. Gemini, without writing anything.

    python diagnose.py            # first 10 candidates
    python diagnose.py 25         # first 25
"""

from __future__ import annotations

import sys

from tracker.auth import build_services, load_user_credentials
from tracker.config import load_settings
from tracker.gmail_client import fetch_message, search_candidate_messages
from tracker.llm import HeuristicClassifier, make_classifier


def diagnose_emails(limit: int = 10) -> None:
    settings = load_settings()
    gmail, _ = build_services(load_user_credentials(settings, interactive=sys.stdin.isatty()))
    heuristic = HeuristicClassifier()
    llm = make_classifier(settings)

    ids = search_candidate_messages(gmail, settings.gmail_search_after)
    print(f"Found {len(ids)} candidate messages; showing {min(limit, len(ids))}\n")
    for i, mid in enumerate(ids[:limit], start=1):
        try:
            email = fetch_message(gmail, mid)
            h = heuristic.classify(email).analysis
            print(f"--- {i}. {email.subject[:90]}")
            print(f"    from:      {email.sender}")
            print(f"    heuristic: {h.category.value:<26} company={h.company!r} role={h.role_title!r}")
            if llm.name != "heuristic":
                c = llm.classify(email)
                g = c.analysis
                usage = f"({c.stats.latency_s:.2f}s, {c.stats.input_tokens}+{c.stats.output_tokens} tok)" if c.stats else ""
                print(f"    gemini:    {g.category.value:<26} company={g.company!r} role={g.role_title!r} {usage}")
                print(f"    summary:   {g.summary}")
        except Exception as exc:
            print(f"--- {i}. error: {exc}")


if __name__ == "__main__":
    diagnose_emails(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
