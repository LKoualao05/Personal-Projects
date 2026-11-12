import asyncio
from datetime import date

import pytest
from mcp import Client

from tracker.config import Settings
from tracker.mcp_server import WRITE_TOOLS, TrackerContext, compute_stats, create_server
from tracker.schemas import ApplicationRow, Category
from tracker.sheets_io import InMemoryStore

from .fakes import FakeClassifier, FakeGmail, analysis, gmail_message

SETTINGS = Settings(sheet_id="t")


def make_server(rows=None, messages=None, results=None):
    store = InMemoryStore(rows or [])
    gmail = FakeGmail(messages or [])
    ctx = TrackerContext(SETTINGS, gmail=gmail, store=store, classifier=FakeClassifier(results or {}))
    return create_server(ctx, today=lambda: date(2026, 11, 11)), store, gmail


def call(server, tool, args=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args or {})
    return asyncio.run(go())


ROWS = [
    ApplicationRow(company="Acme", role_title="SWE Intern", date_applied="2026-09-01", status="Interview",
                   internship="Yes", last_update="2026-10-01"),
    ApplicationRow(company="Globex", role_title="Data Intern", date_applied="2026-10-05", status="Rejected",
                   last_update="2026-10-20"),
    ApplicationRow(company="Initech", role_title="PM Intern", date_applied="2026-10-10", status=""),
]


def test_lists_all_tools_with_annotations():
    server, _, _ = make_server()

    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == {"search_gmail", "read_email", "classify_email", "list_applications",
                          "application_stats", "update_application", "sync_applications"}
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is (name not in WRITE_TOOLS)
    assert "dry_run" in tools["sync_applications"].input_schema["properties"]


def test_list_applications_filters_and_sorts():
    server, _, _ = make_server(ROWS)
    out = call(server, "list_applications", {"status": "rejected"}).structured_content
    assert out["total_matching"] == 1 and out["applications"][0]["company"] == "Globex"
    out = call(server, "list_applications", {"since": "2026-10-06"}).structured_content
    assert [a["company"] for a in out["applications"]] == ["Globex", "Initech"]
    out = call(server, "list_applications", {"internship_only": True}).structured_content
    assert [a["company"] for a in out["applications"]] == ["Acme"]
    assert call(server, "list_applications", {"status": "ghosted"}).is_error


def test_application_stats():
    stats = compute_stats(ROWS)
    assert stats["total_applications"] == 3
    assert stats["by_status"]["Applied"] == 1 and stats["by_status"]["Interview"] == 1
    assert stats["response_rate"] == pytest.approx(0.667, abs=1e-3)
    assert stats["advanced_rate"] == pytest.approx(0.333, abs=1e-3)
    assert stats["applications_by_month"] == {"2026-09": 1, "2026-10": 2}


def test_update_application_writes_sheet():
    server, store, _ = make_server([ApplicationRow(company="Acme", status="Applied")])
    res = call(server, "update_application", {"row_number": 2, "status": "offer", "summary": "Verbal offer!"})
    assert not res.is_error
    row = store.rows[0]
    assert (row.status, row.summary, row.last_update, row.classified_by) == ("Offer", "Verbal offer!", "2026-11-11", "manual")
    assert store.summary[1][0] == "Acme"
    assert call(server, "update_application", {"row_number": 99, "status": "Offer"}).is_error


def test_read_and_search_email():
    msgs = [gmail_message("m1", "Interview availability", "Pick a slot: https://cal.acme.com/x")]
    server, _, gmail = make_server(messages=msgs)
    out = call(server, "read_email", {"message_id": "m1"}).structured_content
    assert out["subject"] == "Interview availability" and out["links"] == ["https://cal.acme.com/x"]
    found = call(server, "search_gmail", {"query": "subject:interview", "max_results": 500}).structured_content
    assert found["result"][0]["id"] == "m1" and gmail.queries == ["subject:interview"]


def test_classify_email_tool():
    msgs = [gmail_message("m1", "Thanks for applying", "We got it")]
    server, _, _ = make_server(messages=msgs, results={"m1": analysis(Category.APPLICATION_CONFIRMATION)})
    out = call(server, "classify_email", {"message_id": "m1"}).structured_content
    assert out["category"] == "application_confirmation" and out["tokens"]["input"] == 100


def test_sync_is_dry_run_by_default():
    msgs = [gmail_message("m1", "Thanks for applying", "We got it")]
    server, store, _ = make_server(messages=msgs, results={"m1": analysis(Category.APPLICATION_CONFIRMATION)})
    out = call(server, "sync_applications").structured_content
    assert out["dry_run"] is True and out["changes"] and store.rows == []
    out = call(server, "sync_applications", {"dry_run": False}).structured_content
    assert len(store.rows) == 1 and "m1" in store.processed


# --- ADK agent wiring ------------------------------------------------------------

def test_agent_builds_with_mcp_toolset_and_vertex(monkeypatch):
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    from agents.tracker_agent.agent import build_agent, is_write_call
    from google.adk.tools.mcp_tool import McpToolset

    agent = build_agent(Settings(gcp_project="p", agent_model="gemini-x"))
    assert agent.name == "internship_tracker" and agent.model == "gemini-x"
    assert isinstance(agent.tools[0], McpToolset)
    import os
    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "TRUE"

    assert is_write_call(row_number=3, status="Offer") is True
    assert is_write_call(dry_run=False) is True
    assert is_write_call(dry_run=True) is False
    assert is_write_call(query="from:acme") is False


def test_agent_uses_http_mcp_when_configured():
    from agents.tracker_agent.agent import _connection
    from google.adk.tools.mcp_tool import StdioConnectionParams, StreamableHTTPConnectionParams

    assert isinstance(_connection(Settings()), StdioConnectionParams)
    assert isinstance(_connection(Settings(tracker_mcp_url="http://x/mcp")), StreamableHTTPConnectionParams)


def test_instruction_has_date_and_injection_guard():
    from agents.tracker_agent.agent import _instruction

    text = _instruction(None)
    assert date.today().isoformat() in text and "untrusted" in text
