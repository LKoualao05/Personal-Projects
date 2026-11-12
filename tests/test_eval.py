from datetime import date

import pytest

from eval.run_eval import (
    CATEGORIES, DATA, Prediction, field_match, load_examples, load_jsonl, main, percentile, render_report,
    run_classifier, score_classification, score_retrieval,
)
from tracker.llm import CallStats, Classification, HeuristicClassifier
from tracker.pricing import cost_usd, price_for, stats_cost_usd
from tracker.rag import HashingEmbedder
from tracker.schemas import EmailAnalysis


# --- pricing ---------------------------------------------------------------------

def test_cost_includes_thinking_and_cached_tokens():
    # 1,000 input (200 cached), 100 output + 50 thinking on gemini-3.5-flash-lite: $0.30 / $2.50 / $0.03 per 1M
    expected = (800 * 0.30 + 200 * 0.03 + 150 * 2.50) / 1e6
    assert cost_usd("gemini-3.5-flash-lite", 1000, 100, 50, 200) == pytest.approx(expected)
    assert cost_usd("unknown-model", 10, 10) is None
    assert cost_usd("heuristic", 10, 10) == 0.0
    assert stats_cost_usd(CallStats("gemini-embedding-001", 0.1, input_tokens=1_000_000)) == pytest.approx(0.15)


def test_introductory_pricing_switches_on_date():
    assert price_for("gemini-3.8-flash", date(2026, 12, 31)).input == 0.75
    assert price_for("gemini-3.8-flash", date(2027, 1, 1)).input == 1.50
    assert price_for("gemini-4-argon", date(2026, 12, 1)).output == 20.00


# --- datasets ----------------------------------------------------------------------

def test_classification_dataset_is_well_formed():
    examples = load_examples()
    assert len(examples) >= 40 and len({e.id for e in examples}) == len(examples)
    counts = {c: sum(e.expected["category"] == c for e in examples) for c in CATEGORIES}
    assert all(n >= 3 for n in counts.values()), counts
    for e in examples:
        is_job = e.expected["category"] != "not_job_related"
        assert bool(e.expected["company"]) == is_job, e.id
        assert ".example" in e.sender, f"{e.id}: use .example domains for synthetic data"
    assert any("injection" in e.tags for e in examples)


def test_retrieval_dataset_is_well_formed():
    jds = load_jsonl(DATA / "job_descriptions.jsonl")
    queries = load_jsonl(DATA / "retrieval.jsonl")
    ids = {j["job_id"] for j in jds}
    assert all(q["expected_job_id"] in ids for q in queries)
    assert {q["kind"] for q in queries} == {"keyword", "paraphrase"}


# --- scoring ---------------------------------------------------------------------------

def pred(exp_cat, pred_cat, latency=0.5, cost=0.001, **fields):
    expected = {"category": exp_cat, "company": fields.get("company"), "role_title": fields.get("role"),
                "job_id": None, "is_internship": None}
    predicted = None if pred_cat is None else {"category": pred_cat, "company": fields.get("pred_company"),
                                               "role_title": fields.get("pred_role")}
    return Prediction("x", expected, predicted, fields.get("tags", []), "s", latency, cost_usd=cost,
                      error="" if pred_cat else "boom")


def test_scoring_metrics():
    preds = [
        pred("rejection", "rejection", 1.0, company="Acme", pred_company="Acme Inc.", role="SWE Intern", pred_role="SWE Intern"),
        pred("rejection", "application_confirmation", 2.0, company="Acme", pred_company="Globex", tags=["tricky"]),
        pred("not_job_related", "not_job_related", 3.0, tags=["tricky"]),
        pred("interview", None),  # error counts as wrong
    ]
    m = score_classification(preds)
    assert m["n"] == 4 and m["errors"] == 1 and m["accuracy"] == 0.5
    assert m["per_class"]["rejection"] == {"precision": 1.0, "recall": 0.5, "f1": pytest.approx(0.667, abs=1e-3), "support": 2}
    assert m["field_accuracy"]["company"] == 0.5 and m["field_accuracy"]["role_title"] == 1.0
    assert m["tag_accuracy"]["tricky"] == 0.5
    assert m["job_relevance_accuracy"] == 0.75
    assert m["latency_s"]["p50"] == 2.0 and m["cost_usd"]["per_1k_emails"] == pytest.approx(1.0)
    assert m["confusion"]["rejection"] == {"rejection": 1, "application_confirmation": 1}
    assert m["confusion"]["interview"] == {"error": 1}
    assert len(m["failures"]) == 2


def test_field_match_rules():
    assert field_match("company", "Litware", "Litware, Inc.")
    assert not field_match("company", "Contoso", "Workday")
    assert field_match("company", None, "anything") is None
    assert field_match("role_title", "Data Analyst Intern", "Data Analyst Intern - Summer")
    assert field_match("job_id", "R-1", "r-1") and not field_match("job_id", "R-1", None)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == 2.5 and percentile([], 95) == 0.0 and percentile([5], 95) == 5


class EchoClassifier:
    """Perfect classifier that reads labels from the dataset (validates the harness wiring)."""

    name = "gemini-3.5-flash-lite"

    def __init__(self, examples):
        self.by_subject = {e.subject: e.expected for e in examples}

    def classify(self, email):
        exp = self.by_subject[email.subject]
        a = EmailAnalysis(category=exp["category"], company=exp["company"], role_title=exp["role_title"],
                          job_id=exp["job_id"], is_internship=exp["is_internship"], summary="s", confidence=1)
        return Classification(a, self.name, CallStats(self.name, 0.2, 1000, 50, 10))


def test_run_classifier_end_to_end_with_costs():
    examples = load_examples()
    m = score_classification(run_classifier(EchoClassifier(examples), examples, workers=4))
    assert m["accuracy"] == 1.0 and m["macro_f1"] == 1.0 and m["field_accuracy"]["company"] == 1.0
    assert m["cost_usd"]["per_request"] == pytest.approx((1000 * 0.30 + 60 * 2.50) / 1e6)


def test_heuristic_baseline_runs_offline():
    examples = load_examples()
    m = score_classification(run_classifier(HeuristicClassifier(), examples))
    assert 0.4 < m["accuracy"] < 0.95 and m["errors"] == 0


def test_retrieval_scoring_with_offline_embedder():
    m = score_retrieval(HashingEmbedder(), load_jsonl(DATA / "job_descriptions.jsonl"), load_jsonl(DATA / "retrieval.jsonl"))
    assert m["recall@3"] >= m["recall@1"] > 0.3 and m["by_kind"]["keyword"]["recall@1"] >= 0.75
    assert m["cost_usd"]["total"] == 0.0


def test_report_and_cli_gate(tmp_path, capsys):
    report = tmp_path / "REPORT.md"
    assert main(["classify", "--models", "heuristic", "--no-save", "--report", str(report)]) == 0
    assert "| `heuristic` |" in capsys.readouterr().out
    assert main(["classify", "--models", "heuristic", "--no-save", "--fail-under", "0.99"]) == 1
    text = render_report({"generated_at": "now", "classification": {"m": score_classification([pred("offer", "offer")])}})
    assert "## Email classification (1 labeled emails)" in text
