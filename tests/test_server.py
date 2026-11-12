"""Cloud Run app: routes, sync endpoint, MCP over real HTTP, ADK agent API."""

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client

from tracker.config import Settings
from tracker.mcp_server import TrackerContext
from tracker.schemas import ApplicationRow
from tracker.server import create_app
from tracker.sheets_io import InMemoryStore

from .fakes import FakeGmail

SETTINGS = Settings(sheet_id="t", gcp_project="", token_path="/nonexistent/token.json")


def ctx():
    store = InMemoryStore([ApplicationRow(company="Acme", role_title="SWE Intern", status="Interview")])
    return TrackerContext(SETTINGS, gmail=FakeGmail([]), store=store)


def test_healthz_and_sync_endpoint():
    calls = []

    def runner(settings, **kw):
        calls.append(kw)
        return {"ok": True, **kw}

    with TestClient(create_app(SETTINGS, mcp_context=ctx(), sync_runner=runner, with_agent=False)) as client:
        h = client.get("/tracker/status").json()
        assert h["ok"] and h["token_file_present"] is False
        assert client.get("/healthz").json() == h
        r = client.post("/tasks/sync?dry_run=true&limit=5")
        assert r.status_code == 200 and calls == [{"dry_run": True, "limit": 5, "backfill": False}]
        assert client.post("/tasks/sync").json()["limit"] == 500  # default cap per scheduled run


def test_sync_errors_and_concurrency():
    gate = threading.Event()

    def slow(settings, **kw):
        gate.wait(5)
        return {}

    def boom(settings, **kw):
        raise RuntimeError("token expired")

    with TestClient(create_app(SETTINGS, mcp_context=ctx(), sync_runner=boom, with_agent=False)) as client:
        r = client.post("/tasks/sync")
        assert r.status_code == 500 and "token expired" in r.json()["detail"]

    app = create_app(SETTINGS, mcp_context=ctx(), sync_runner=slow, with_agent=False)
    with TestClient(app) as client:
        t = threading.Thread(target=lambda: client.post("/tasks/sync"))
        t.start()
        time.sleep(0.3)
        assert client.post("/tasks/sync").status_code == 409
        gate.set()
        t.join()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    app = create_app(SETTINGS, mcp_context=ctx(), sync_runner=lambda s, **k: {}, with_agent=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_mcp_over_streamable_http(live_server):
    async def go():
        async with Client(f"{live_server}/mcp/") as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            stats = await client.call_tool("application_stats", {})
            return tools, stats
    tools, stats = asyncio.run(go())
    assert {"search_gmail", "list_applications", "search_job_descriptions"} <= tools
    assert stats.structured_content["by_status"]["Interview"] == 1


def test_adk_agent_api_is_mounted():
    with TestClient(create_app(SETTINGS, mcp_context=ctx(), sync_runner=lambda s, **k: {})) as client:
        assert client.get("/list-apps").json() == ["tracker_agent"]
        assert client.get("/tracker/status").json()["ok"] is True  # not shadowed by ADK's routes
        assert client.get("/health").status_code == 200           # ADK's own liveness route
        assert client.get("/dev-ui/").status_code == 200
