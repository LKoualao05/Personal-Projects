import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from mcp import Client

from tracker.config import Settings
from tracker.mcp_server import TrackerContext, create_server
from tracker.pipeline import run_sync
from tracker.rag import HashingEmbedder, JobDescriptionIndex, LocalVectorStore, chunk_text, job_key
from tracker.rag.sources import endpoint_for, find_job_posting_urls, looks_like_job_description, parse_posting
from tracker.rag.stores import FirestoreVectorStore, JDChunk
from tracker.schemas import Category, EmailMessage
from tracker.sheets_io import DryRunStore, InMemoryStore

from .fakes import FakeClassifier, FakeGmail, analysis, gmail_message

ML_JD = """About the role
You will train computer vision models with PyTorch and deploy them on GPUs.

Qualifications
- Experience with PyTorch, CUDA and deep learning
- Python and Linux"""
FE_JD = """Responsibilities
Build user interfaces in React and TypeScript. Work with designers on accessibility.

Requirements
- JavaScript, React, CSS"""
FIN_JD = """What you'll do
Build Excel financial models and support quarterly forecasting.

Qualifications
- Accounting coursework, advanced Excel, PowerPoint"""


def make_index(http=None, answer_client=None, path=None):
    return JobDescriptionIndex(HashingEmbedder(), LocalVectorStore(path), http=http,
                               answer_client=answer_client, answer_model="gemini-test")


def seeded_index(**kw):
    idx = make_index(**kw)
    idx.add("Acme", "ML Intern", ML_JD, job_id="R-1")
    idx.add("Globex", "Frontend Intern", FE_JD)
    idx.add("Initech", "Finance Intern", FIN_JD)
    return idx


# --- chunking ----------------------------------------------------------------

def test_chunking_packs_paragraphs_and_overlaps_long_text():
    assert chunk_text("") == []
    assert chunk_text("Short JD.") == ["Short JD."]
    long = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(long, max_chars=500, overlap=80)
    assert len(chunks) > 5 and all(len(c) <= 500 for c in chunks)
    assert chunks[0].split()[-1] in chunks[1]  # overlap carries context across the boundary
    bullets = chunk_text("Intro\n- a\n- b", max_chars=1000)
    assert bullets == ["Intro\n- a\n- b"]


# --- retrieval -------------------------------------------------------------------

def test_semantic_search_ranks_relevant_jd_first():
    idx = seeded_index()
    assert idx.search("deep learning with pytorch", k=1)[0].chunk.company == "Acme"
    assert idx.search("react typescript ui", k=1)[0].chunk.company == "Globex"
    assert idx.search("excel forecasting", k=1)[0].chunk.company == "Initech"
    only = idx.search("python", k=5, company="globex")
    assert only and all(h.chunk.company == "Globex" for h in only)


def test_upsert_replaces_previous_chunks_and_persists(tmp_path):
    path = tmp_path / "store.json"
    idx = make_index(path=path)
    idx.add("Acme", "ML Intern", ML_JD + "\n\n" + "filler " * 400, job_id="R-1")
    first = idx.store.count()
    idx.add("Acme", "ML Intern", "Updated: short JD", job_id="R-1")
    assert idx.store.count() == 1 < first
    reloaded = LocalVectorStore(path)
    assert reloaded.count() == 1 and reloaded.has_job(job_key("Acme", "ML Intern", "R-1"))


def test_job_key_is_stable_across_formatting():
    assert job_key("Acme, Inc.", "ML Intern", "R-1") == job_key("ACME", "intern ml", "r-1")
    assert job_key("Acme", "ML Intern") != job_key("Acme", "Data Intern")


# --- sources ----------------------------------------------------------------------

def test_only_job_board_links_are_followed():
    links = [
        "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=abc",
        "https://click.linkedin.com/abc/track?token=secret",
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/ML-Intern_R-1",
        "https://www.linkedin.com/jobs/view/3901234567/",
        "https://example.com/unsubscribe",
    ]
    assert find_job_posting_urls(links) == [
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/ML-Intern_R-1",
        "https://www.linkedin.com/jobs/view/3901234567",
    ]


def test_endpoints_use_public_json_apis():
    assert endpoint_for("https://boards.greenhouse.io/acme/jobs/1") == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1", "greenhouse")
    assert endpoint_for("https://jobs.lever.co/acme/0b5a3c1e-1111-2222-3333-444455556666")[1] == "lever"
    assert endpoint_for("https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/ML-Intern_R-1") == (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/NYC/ML-Intern_R-1", "workday")
    assert endpoint_for("https://www.linkedin.com/jobs/view/3901234567")[0].endswith("/jobPosting/3901234567")
    assert endpoint_for("https://jobs.example.com/123")[1] == "html"


def test_parse_posting_formats():
    title, text = parse_posting("greenhouse", {"title": "ML Intern", "content": "&lt;p&gt;Train &lt;b&gt;models&lt;/b&gt;&lt;/p&gt;"})
    assert (title, text) == ("ML Intern", "Train models")
    title, text = parse_posting("lever", {"text": "FE Intern", "descriptionPlain": "Build UIs",
                                          "lists": [{"text": "Requirements", "content": "<li>React</li>"}]})
    assert title == "FE Intern" and "Build UIs" in text and "Requirements\nReact" in text
    title, text = parse_posting("workday", {"jobPostingInfo": {"title": "Fin Intern", "jobDescription": "<p>Excel</p>"}})
    assert (title, text) == ("Fin Intern", "Excel")


def test_looks_like_job_description():
    assert looks_like_job_description(("Responsibilities: build. Qualifications: python. " * 20))
    assert not looks_like_job_description("Thanks for applying! We'll be in touch.")


# --- ingestion from email ----------------------------------------------------------

def greenhouse_http(calls):
    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if "boards-api.greenhouse.io" in str(request.url):
            return httpx.Response(200, json={"title": "ML Intern", "content": json.dumps(ML_JD * 3)[1:-1]})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def email_with(links, body="Thanks for applying!"):
    return EmailMessage(id="m1", thread_id="t", internal_date_ms=0, subject="Thanks", sender="x", body=body, links=links)


def test_ingest_email_fetches_posting_once():
    calls = []
    idx = make_index(http=greenhouse_http(calls))
    email = email_with(["https://click.linkedin.com/track?t=1", "https://boards.greenhouse.io/acme/jobs/42"])
    a = analysis(Category.APPLICATION_CONFIRMATION, company="Acme", role="ML Intern")
    assert idx.ingest_email(email, a) > 0
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/42"]
    hit = idx.search("pytorch", k=1)[0].chunk
    assert (hit.source, hit.source_url, hit.message_id) == ("posting", "https://boards.greenhouse.io/acme/jobs/42", "m1")
    assert idx.ingest_email(email, a) == 0 and len(calls) == 1  # already indexed: no refetch


def test_ingest_email_falls_back_to_body_and_skips_noise():
    idx = make_index(http=greenhouse_http([]))
    body_jd = "Thanks for applying. " + ("Responsibilities: ship features. Qualifications: Python, SQL. " * 15)
    a = analysis(Category.APPLICATION_CONFIRMATION, company="Hooli", role="Data Intern")
    assert idx.ingest_email(email_with([], body_jd), a) > 0
    assert idx.search("sql", k=1)[0].chunk.source == "email"
    assert idx.ingest_email(email_with([]), analysis(Category.NOT_JOB_RELATED, company=None)) == 0
    assert idx.ingest_email(email_with([]), analysis(Category.APPLICATION_CONFIRMATION, company="Nope")) == 0


# --- grounded answers ----------------------------------------------------------------

class FakeAnswerClient:
    def __init__(self):
        self.models = self
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(contents)
        return SimpleNamespace(text="Acme's ML Intern role uses PyTorch [1].",
                               usage_metadata=SimpleNamespace(prompt_token_count=300, candidates_token_count=20))


def test_answer_cites_retrieved_excerpts():
    client = FakeAnswerClient()
    idx = seeded_index(answer_client=client)
    ans = idx.answer("Which roles use PyTorch?", k=2)
    assert "[1]" in ans.answer and ans.sources[0]["company"] == "Acme" and ans.sources[0]["n"] == 1
    assert "[1] Acme — ML Intern" in client.calls[0] and ans.stats.input_tokens == 300
    assert "No job descriptions" in make_index(answer_client=client).answer("anything").answer


# --- Firestore store (mocked client) ---------------------------------------------------

def test_firestore_store_upsert_and_search():
    from google.cloud.firestore_v1.vector import Vector

    client = MagicMock()
    col = client.collection.return_value
    col.where.return_value.get.return_value = []
    store = FirestoreVectorStore("proj", client=client)
    chunk = JDChunk(job_key="k1", company="Acme", role_title="ML Intern", chunk_index=0, text="pytorch", embedding=[0.1, 0.2])
    store.upsert([chunk])
    doc_id, data = col.document.call_args[0][0], client.batch.return_value.set.call_args[0][1]
    assert doc_id == "k1_000" and isinstance(data["embedding"], Vector) and data["company"] == "Acme"

    snap = MagicMock()
    snap.to_dict.return_value = chunk.metadata() | {"embedding": Vector([0.1, 0.2]), "vector_distance": 0.25}
    other = MagicMock()
    other.to_dict.return_value = (chunk.metadata() | {"company": "Globex", "vector_distance": 0.1})
    col.find_nearest.return_value.get.return_value = [other, snap]
    hits = store.search([0.1, 0.2], k=3, company="acme")
    kwargs = col.find_nearest.call_args.kwargs
    assert kwargs["vector_field"] == "embedding" and kwargs["limit"] == 15 and kwargs["distance_measure"].name == "COSINE"
    assert len(hits) == 1 and hits[0].chunk.company == "Acme" and abs(hits[0].score - 0.75) < 1e-9


# --- MCP tools + pipeline hook ---------------------------------------------------------

def mcp_call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go())


def test_mcp_rag_tools():
    idx = seeded_index()
    server = create_server(TrackerContext(Settings(), gmail=FakeGmail([]), store=InMemoryStore(), jd_index=idx))
    out = mcp_call(server, "search_job_descriptions", {"query": "react ui", "k": 2}).structured_content
    assert out["results"][0]["company"] == "Globex" and out["results"][0]["n"] == 1
    out = mcp_call(server, "add_job_description", {"company": "Hooli", "role_title": "SRE Intern", "text": "Kubernetes on-call"})
    assert out.structured_content["chunks_indexed"] == 1
    assert mcp_call(server, "add_job_description", {"company": "Hooli"}).is_error

    unconfigured = create_server(TrackerContext(Settings(rag_enabled=False), gmail=FakeGmail([]), store=InMemoryStore()))
    res = mcp_call(unconfigured, "search_job_descriptions", {"query": "x"})
    assert res.is_error and "not configured" in res.content[0].text


def test_pipeline_indexes_jds_only_for_job_emails_and_not_in_dry_run():
    msgs = [gmail_message("a", "Thanks for applying", "b"), gmail_message("n", "Newsletter", "b", day=1)]
    clf = lambda: FakeClassifier({"a": analysis(Category.APPLICATION_CONFIRMATION),  # noqa: E731
                                  "n": analysis(Category.NOT_JOB_RELATED, company=None)})
    seen = []
    hook = lambda email, c: seen.append(email.id) or 3  # noqa: E731
    res = run_sync(Settings(), FakeGmail(msgs), InMemoryStore(), clf(), on_classified=hook, log=lambda *_: None)
    assert seen == ["a"] and res.jd_chunks_indexed == 3
    seen.clear()
    run_sync(Settings(), FakeGmail(msgs), DryRunStore(InMemoryStore()), clf(), on_classified=hook, dry_run=True,
             log=lambda *_: None)
    assert seen == []
