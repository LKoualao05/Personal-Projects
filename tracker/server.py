"""Cloud Run service: ADK agent API + dev UI, the tracker MCP server, and the daily sync.

    uvicorn tracker.server:app --port 8080

Routes
    GET  /tracker/status    config summary (also /healthz locally; Cloud Run's front end
                            reserves paths ending in "z", and ADK already serves /health)
    POST /tasks/sync        Gmail -> Gemini -> Sheets (+ JD indexing); called daily by Cloud Scheduler
    /mcp/                   tracker MCP server over streamable HTTP (stateless, JSON responses)
    /dev-ui/, /run, /run_sse, /apps/...   ADK agent API and dev UI (google.adk get_fast_api_app)

The service is deployed with --no-allow-unauthenticated, so Cloud Run IAM
authenticates every request (your Google account through `gcloud run services
proxy`, and the Cloud Scheduler service account through an OIDC token).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Mount

from . import __version__
from .config import REPO_ROOT, Settings, load_settings

log = logging.getLogger("tracker.server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SyncRunner = Callable[..., dict[str, Any]]


def default_sync_runner(settings: Settings, *, dry_run: bool, limit: Optional[int], backfill: bool) -> dict[str, Any]:
    from .auth import build_services, load_user_credentials
    from .llm import make_classifier
    from .pipeline import run_sync
    from .rag import make_jd_index
    from .sheets_io import DryRunStore, GoogleSheetsStore

    gmail, sheets = build_services(load_user_credentials(settings))
    store = GoogleSheetsStore(sheets, settings)
    jd_index = make_jd_index(settings, log=log.info)
    result = run_sync(
        settings, gmail, DryRunStore(store) if dry_run else store, make_classifier(settings, log=log.info),
        limit=limit, backfill=backfill, dry_run=dry_run, log=log.info,
        on_classified=(lambda e, c: jd_index.ingest_email(e, c.analysis)) if jd_index else None,
    )
    log.info(result.report())
    return {
        "dry_run": dry_run, "classifier": result.classifier, "candidates": result.candidates,
        "classified": result.classified, "new_applications": result.new_applications,
        "status_updates": result.status_updates, "backfilled": result.backfilled,
        "jd_chunks_indexed": result.jd_chunks_indexed, "errors": result.errors[:20],
        "changes": result.changes[:100], "report": result.report(),
    }


def create_app(settings: Optional[Settings] = None, *, mcp_context: Any = None,
               sync_runner: SyncRunner = default_sync_runner, with_agent: bool = True) -> FastAPI:
    settings = settings or load_settings()
    from .mcp_server import TrackerContext, create_server

    mcp = create_server(mcp_context or TrackerContext(settings))
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True, host="0.0.0.0",
        # DNS-rebinding protection guards localhost servers from malicious web
        # pages. Behind Cloud Run IAM the Host is *.run.app and IAM is the auth.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():  # sub-app lifespans don't run when mounted
            yield

    if with_agent:
        from google.adk.cli.fast_api import get_fast_api_app

        app = get_fast_api_app(
            agents_dir=str(REPO_ROOT / "agents"), web=True, use_local_storage=False,
            session_service_uri="memory://", artifact_service_uri="memory://", lifespan=lifespan,
        )
    else:
        app = FastAPI(lifespan=lifespan)
    app.title = "Gmail Internship Tracker"
    app.version = __version__

    sync_lock = threading.Lock()

    @app.get("/tracker/status")
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "llm": settings.llm_enabled, "model": settings.gemini_model,
                "agent_model": settings.agent_model, "rag": settings.rag_enabled, "vector_store": settings.vector_store,
                "token_file_present": Path(settings.resolve(settings.token_path)).exists()}

    @app.post("/tasks/sync")
    def sync(dry_run: bool = False, limit: Optional[int] = 500, backfill: bool = False) -> dict[str, Any]:
        """Run one sync. ``limit`` caps Gemini calls per run so a large backlog
        is worked through over several days within Cloud Scheduler's deadline."""
        if not sync_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A sync is already running.")
        try:
            return sync_runner(settings, dry_run=dry_run, limit=limit, backfill=backfill)
        except Exception as exc:
            log.exception("sync failed")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            sync_lock.release()

    app.router.routes.append(Mount("/mcp", app=mcp_app))
    return app


def __getattr__(name: str):  # lazy so importing the module (tests) doesn't build the app
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
