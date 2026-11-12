"""Evaluate the email classifier and the job-description retriever.

Classification: accuracy, macro-F1, per-category recall, field accuracy,
latency (p50/p95) and cost per request, per model.
Retrieval: recall@1/@3 and MRR (overall and on paraphrased queries),
query latency and embedding cost, per embedder.

    python -m eval.run_eval classify --models heuristic gemini-3.5-flash-lite gemini-3.8-flash
    python -m eval.run_eval retrieval --embedders hashing gemini-embedding-001
    python -m eval.run_eval all                     # both, with the configured models
    python -m eval.run_eval classify --models heuristic --fail-under 0.5   # CI gate

Datasets in eval/datasets are synthetic (fictional companies, .example domains),
so no real email ever enters the repo. Results: eval/results/*.json (git-ignored)
and a Markdown report (default eval/REPORT.md).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tracker.config import Settings, load_settings
from tracker.llm import EmailClassifier, GeminiClassifier, HeuristicClassifier
from tracker.pipeline import normalize_company, role_similarity
from tracker.pricing import PRICING_AS_OF, cost_usd
from tracker.schemas import Category, EmailMessage

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "datasets"
CATEGORIES = [c.value for c in Category]
NOT_JOB = Category.NOT_JOB_RELATED.value


# --------------------------------------------------------------------------- data

@dataclass
class Example:
    id: str
    sender: str
    subject: str
    body: str
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)

    def to_email(self) -> EmailMessage:
        return EmailMessage(id=self.id, thread_id=self.id, internal_date_ms=0, subject=self.subject,
                            sender=self.sender, snippet=self.body[:200], body=self.body)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_examples(path: Path = DATA / "classification.jsonl") -> list[Example]:
    return [Example(**d) for d in load_jsonl(path)]


# --------------------------------------------------------------------- classification

@dataclass
class Prediction:
    id: str
    expected: dict[str, Any]
    predicted: Optional[dict[str, Any]]
    tags: list[str]
    subject: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Optional[float] = None
    error: str = ""

    @property
    def predicted_category(self) -> Optional[str]:
        return self.predicted["category"] if self.predicted else None


def run_classifier(classifier: EmailClassifier, examples: list[Example], workers: int = 4) -> list[Prediction]:
    def one(ex: Example) -> Prediction:
        start = time.perf_counter()
        try:
            c = classifier.classify(ex.to_email())
        except Exception as exc:
            return Prediction(ex.id, ex.expected, None, ex.tags, ex.subject, time.perf_counter() - start,
                              error=f"{type(exc).__name__}: {exc}")
        s = c.stats
        p = Prediction(ex.id, ex.expected, c.analysis.model_dump(mode="json"), ex.tags, ex.subject,
                       s.latency_s if s else time.perf_counter() - start)
        if s:
            p.input_tokens, p.output_tokens = s.input_tokens, s.output_tokens
            p.thinking_tokens, p.cached_tokens = s.thinking_tokens, s.cached_tokens
            p.cost_usd = cost_usd(classifier.name, s.input_tokens, s.output_tokens, s.thinking_tokens, s.cached_tokens)
        return p

    n = 1 if classifier.name == "heuristic" else max(1, workers)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(one, examples))


def field_match(name: str, expected: Any, predicted: Any) -> Optional[bool]:
    """Whether a predicted field is right; None when the example doesn't score it."""
    if name == "company":
        if expected is None:
            return None
        e, p = normalize_company(expected), normalize_company(predicted)
        return bool(p) and (e == p or e in p or p in e)
    if name == "role_title":
        if expected is None:
            return None
        return role_similarity(expected, predicted) >= 0.6
    if name == "job_id":
        if expected is None:
            return None
        return (predicted or "").strip().lower() == expected.lower()
    if name == "is_internship":
        return None if expected is None else predicted == expected
    raise ValueError(name)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def score_classification(preds: list[Prediction]) -> dict[str, Any]:
    n = len(preds)
    ok = [p for p in preds if not p.error]
    correct = [p for p in preds if p.predicted_category == p.expected["category"]]

    per_class = {}
    for cat in CATEGORIES:
        tp = sum(1 for p in preds if p.expected["category"] == cat and p.predicted_category == cat)
        fp = sum(1 for p in preds if p.expected["category"] != cat and p.predicted_category == cat)
        fn = sum(1 for p in preds if p.expected["category"] == cat and p.predicted_category != cat)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per_class[cat] = {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "support": tp + fn}
    supported = [v for v in per_class.values() if v["support"]]

    confusion: dict[str, Counter] = defaultdict(Counter)
    for p in preds:
        confusion[p.expected["category"]][p.predicted_category or "error"] += 1

    fields = {}
    for name in ("company", "role_title", "job_id", "is_internship"):
        scored = [field_match(name, p.expected.get(name), (p.predicted or {}).get(name))
                  for p in preds if p.expected["category"] != NOT_JOB]
        scored = [s for s in scored if s is not None]
        fields[name] = round(sum(scored) / len(scored), 3) if scored else None

    tags: dict[str, list[bool]] = defaultdict(list)
    for p in preds:
        for t in p.tags:
            tags[t].append(p.predicted_category == p.expected["category"])

    latencies = [p.latency_s for p in ok]
    costs = [p.cost_usd for p in ok if p.cost_usd is not None]
    mean_cost = statistics.fmean(costs) if costs else None
    return {
        "n": n,
        "errors": n - len(ok),
        "accuracy": round(len(correct) / n, 3) if n else 0.0,
        "macro_f1": round(statistics.fmean(v["f1"] for v in supported), 3) if supported else 0.0,
        "job_relevance_accuracy": round(sum(
            (p.expected["category"] != NOT_JOB) == (p.predicted_category not in (None, NOT_JOB)) for p in preds) / n, 3) if n else 0.0,
        "per_class": per_class,
        "field_accuracy": fields,
        "tag_accuracy": {t: round(sum(v) / len(v), 3) for t, v in sorted(tags.items())},
        "latency_s": {"mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                      "p50": round(percentile(latencies, 50), 3), "p95": round(percentile(latencies, 95), 3),
                      "max": round(max(latencies), 3) if latencies else 0.0},
        "tokens_mean": {k: round(statistics.fmean(getattr(p, k) for p in ok), 1) if ok else 0.0
                        for k in ("input_tokens", "output_tokens", "thinking_tokens")},
        "cost_usd": {"total": round(sum(costs), 6) if costs else None,
                     "per_request": round(mean_cost, 7) if mean_cost is not None else None,
                     "per_1k_emails": round(mean_cost * 1000, 4) if mean_cost is not None else None},
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "failures": [{"id": p.id, "subject": p.subject, "expected": p.expected["category"],
                      "predicted": p.predicted_category, "error": p.error}
                     for p in preds if p.predicted_category != p.expected["category"]][:15],
    }


# -------------------------------------------------------------------------- retrieval

def score_retrieval(embedder, jds: list[dict], queries: list[dict], k_values: Iterable[int] = (1, 3)) -> dict[str, Any]:
    from tracker.rag import JobDescriptionIndex, LocalVectorStore

    index = JobDescriptionIndex(embedder, LocalVectorStore())
    start = time.perf_counter()
    for jd in jds:
        index.add(jd["company"], jd["role_title"], jd["text"], job_id=jd["job_id"])
    index_s = time.perf_counter() - start
    doc_tokens = embedder.tokens_used

    ranks: list[Optional[int]] = []
    kinds: dict[str, list[Optional[int]]] = defaultdict(list)
    latencies = []
    misses = []
    for q in queries:
        t = time.perf_counter()
        hits = index.search(q["query"], k=20)
        latencies.append(time.perf_counter() - t)
        ranked = list(dict.fromkeys(h.chunk.job_id for h in hits))  # dedupe chunks -> jobs
        rank = ranked.index(q["expected_job_id"]) + 1 if q["expected_job_id"] in ranked else None
        ranks.append(rank)
        kinds[q.get("kind", "all")].append(rank)
        if rank != 1:
            misses.append({"query": q["query"], "expected": q["expected_job_id"], "top": ranked[:3]})

    def recall(rs, k):
        return round(sum(1 for r in rs if r is not None and r <= k) / len(rs), 3) if rs else 0.0

    query_tokens = embedder.tokens_used - doc_tokens
    total_cost = cost_usd(embedder.name, input_tokens=embedder.tokens_used)
    return {
        "n_queries": len(queries), "n_jobs": len(jds), "chunks": index.store.count(),
        **{f"recall@{k}": recall(ranks, k) for k in k_values},
        "mrr": round(statistics.fmean(1 / r if r else 0.0 for r in ranks), 3) if ranks else 0.0,
        "by_kind": {kind: {f"recall@{k}": recall(rs, k) for k in k_values} for kind, rs in sorted(kinds.items())},
        "query_latency_s": {"mean": round(statistics.fmean(latencies), 3), "p95": round(percentile(latencies, 95), 3)},
        "index_time_s": round(index_s, 2),
        "embedding_tokens": {"documents": doc_tokens, "queries": query_tokens},
        "cost_usd": {"total": round(total_cost, 6) if total_cost is not None else None,
                     "per_query": round(cost_usd(embedder.name, input_tokens=query_tokens) / len(queries), 8)
                     if total_cost is not None and queries else None},
        "misses": misses,
    }


# ----------------------------------------------------------------------------- report

def _money(v: Optional[float], digits: int = 5) -> str:
    return "n/a" if v is None else f"${v:.{digits}f}"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_report(results: dict[str, Any]) -> str:
    lines = [f"# Eval report", "",
             f"Generated {results['generated_at']} · pricing as of {PRICING_AS_OF} · synthetic datasets in `eval/datasets/`", ""]
    cls = results.get("classification")
    if cls:
        n = next(iter(cls.values()))["n"]
        lines += [f"## Email classification ({n} labeled emails)", "",
                  "| Model | Accuracy | Macro-F1 | Job vs not-job | Company | Role | p50 latency | p95 latency | Cost / request | Cost / 1k emails | Errors |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for model, m in cls.items():
            f = m["field_accuracy"]
            lines.append(f"| `{model}` | {_pct(m['accuracy'])} | {m['macro_f1']:.3f} | {_pct(m['job_relevance_accuracy'])} | "
                         f"{_pct(f['company'])} | {_pct(f['role_title'])} | {m['latency_s']['p50']:.2f}s | "
                         f"{m['latency_s']['p95']:.2f}s | {_money(m['cost_usd']['per_request'], 6)} | "
                         f"{_money(m['cost_usd']['per_1k_emails'], 3)} | {m['errors']} |")
        lines += ["", "**Recall by category**", "", "| Category | " + " | ".join(f"`{m}`" for m in cls) + " |",
                  "|---|" + "---|" * len(cls)]
        for cat in CATEGORIES:
            support = next(iter(cls.values()))["per_class"][cat]["support"]
            lines.append(f"| {cat} ({support}) | " + " | ".join(_pct(m["per_class"][cat]["recall"]) for m in cls.values()) + " |")
        tags = sorted({t for m in cls.values() for t in m["tag_accuracy"]})
        if tags:
            lines += ["", "**Accuracy on hard-case subsets**", "", "| Subset | " + " | ".join(f"`{m}`" for m in cls) + " |",
                      "|---|" + "---|" * len(cls)]
            for t in tags:
                lines.append(f"| {t} | " + " | ".join(_pct(m["tag_accuracy"].get(t)) for m in cls.values()) + " |")
        token_parts = [f"`{model}` {m['tokens_mean']['input_tokens']:.0f} in / {m['tokens_mean']['output_tokens']:.0f} out / "
                       f"{m['tokens_mean']['thinking_tokens']:.0f} thinking" for model, m in cls.items() if m["tokens_mean"]["input_tokens"]]
        if token_parts:
            lines += ["", "**Tokens per request (mean)**: " + "; ".join(token_parts)]
        for model, m in cls.items():
            if m["failures"]:
                lines += ["", f"<details><summary>Misclassified by <code>{model}</code> ({len(m['failures'])})</summary>", ""]
                lines += [f"- `{f['id']}` {f['subject'][:70]} — expected **{f['expected']}**, got **{f['predicted'] or f['error'][:60]}**"
                          for f in m["failures"]]
                lines += ["", "</details>"]
    ret = results.get("retrieval")
    if ret:
        r0 = next(iter(ret.values()))
        lines += ["", f"## Job-description retrieval ({r0['n_queries']} queries over {r0['n_jobs']} JDs, {r0['chunks']} chunks)", "",
                  "| Embedder | Recall@1 | Recall@3 | MRR | Recall@1 (keyword) | Recall@1 (paraphrase) | Query latency | Eval cost |",
                  "|---|---|---|---|---|---|---|---|"]
        for name, m in ret.items():
            kinds = m["by_kind"]
            lines.append(f"| `{name}` | {_pct(m['recall@1'])} | {_pct(m['recall@3'])} | {m['mrr']:.3f} | "
                         f"{_pct(kinds.get('keyword', {}).get('recall@1'))} | {_pct(kinds.get('paraphrase', {}).get('recall@1'))} | "
                         f"{m['query_latency_s']['mean'] * 1000:.0f} ms | {_money(m['cost_usd']['total'], 6)} |")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------------- CLI

def make_classifier(name: str, settings: Settings, thinking_level: str = "") -> EmailClassifier:
    if name == "heuristic":
        return HeuristicClassifier()
    if not settings.gcp_project:
        raise SystemExit(f"Model {name!r} needs GCP_PROJECT (Vertex AI).")
    return GeminiClassifier(model=name, project=settings.gcp_project, location=settings.gemini_location,
                            thinking_level=thinking_level or settings.gemini_thinking_level)


def make_embedder(name: str, settings: Settings):
    from tracker.rag import GeminiEmbedder, HashingEmbedder

    if name == "hashing":
        return HashingEmbedder()
    if not settings.gcp_project:
        raise SystemExit(f"Embedder {name!r} needs GCP_PROJECT (Vertex AI).")
    return GeminiEmbedder(name, settings.embedding_dims, settings.gcp_project, settings.embedding_location)


def main(argv: Optional[list[str]] = None) -> int:
    settings = load_settings()
    p = argparse.ArgumentParser(prog="python -m eval.run_eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("suite", choices=["classify", "retrieval", "all"])
    p.add_argument("--models", nargs="+", default=["heuristic", settings.gemini_model])
    p.add_argument("--embedders", nargs="+", default=["hashing", settings.embedding_model])
    p.add_argument("--dataset", type=Path, default=DATA / "classification.jsonl")
    p.add_argument("--limit", type=int, default=None, help="use only the first N examples")
    p.add_argument("--workers", type=int, default=4, help="parallel Gemini requests")
    p.add_argument("--thinking-level", default="", help="MINIMAL | LOW | MEDIUM | HIGH")
    p.add_argument("--report", type=Path, default=ROOT / "REPORT.md", help="Markdown report path")
    p.add_argument("--no-save", action="store_true", help="don't write the JSON results or the report")
    p.add_argument("--fail-under", type=float, default=None, help="exit 1 if any model's accuracy is below this")
    args = p.parse_args(argv)

    results: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    if args.suite in ("classify", "all"):
        examples = load_examples(args.dataset)[: args.limit]
        results["classification"] = {}
        for model in args.models:
            print(f"classifying {len(examples)} emails with {model}...", file=sys.stderr)
            preds = run_classifier(make_classifier(model, settings, args.thinking_level), examples, args.workers)
            results["classification"][model] = score_classification(preds)
            results.setdefault("predictions", {})[model] = [asdict(x) for x in preds]
    if args.suite in ("retrieval", "all"):
        jds, queries = load_jsonl(DATA / "job_descriptions.jsonl"), load_jsonl(DATA / "retrieval.jsonl")
        results["retrieval"] = {}
        for name in args.embedders:
            print(f"retrieval eval with {name}...", file=sys.stderr)
            results["retrieval"][name] = score_retrieval(make_embedder(name, settings), jds, queries)

    report = render_report(results)
    print(report)
    if not args.no_save:
        out = ROOT / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(results, indent=2))
        args.report.write_text(report)
        print(f"saved {out.relative_to(ROOT.parent)} and {args.report.relative_to(ROOT.parent) if args.report.is_relative_to(ROOT.parent) else args.report}",
              file=sys.stderr)

    if args.fail_under is not None and "classification" in results:
        low = {m: r["accuracy"] for m, r in results["classification"].items() if r["accuracy"] < args.fail_under}
        if low:
            print(f"FAIL: accuracy below {args.fail_under}: {low}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
