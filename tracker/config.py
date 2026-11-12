"""Settings loaded from config.json, overridable by environment variables.

Every setting can be overridden with an environment variable of the same name in
upper case (e.g. ``GEMINI_MODEL=gemini-3.8-flash``). That is how Cloud Run and
CI configure the app without editing config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.json"


@dataclass(frozen=True)
class Settings:
    sheet_id: str = ""
    sheet_name: str = "Applications"
    processed_sheet_name: str = "ProcessedMessageIds"
    summary_sheet_name: str = "CompanySummary"
    timezone: str = "America/New_York"
    # Gmail search lower bound, in Gmail's YYYY/MM/DD format.
    gmail_search_after: str = "2025/01/01"

    # Gemini on Vertex AI. When use_llm is false (or no project is set) the
    # tracker falls back to the keyword/regex heuristics.
    use_llm: bool = True
    gcp_project: str = ""
    gemini_location: str = "global"
    gemini_model: str = "gemini-3.5-flash-lite"
    # Optional: MINIMAL | LOW | MEDIUM | HIGH. Empty means the model default.
    gemini_thinking_level: str = ""
    llm_max_workers: int = 6

    # ADK agent (agents/tracker_agent). Tool use benefits from a stronger
    # model than the high-volume classifier.
    agent_model: str = "gemini-3.5-flash"
    # Ask the user before the agent writes to the sheet (ADK tool confirmation).
    agent_confirm_writes: bool = True
    # Optional URL of a remote tracker MCP server (streamable HTTP). Empty
    # means the agent launches ``python -m tracker.mcp_server`` over stdio.
    tracker_mcp_url: str = ""

    # RAG over job descriptions.
    rag_enabled: bool = True
    # "hashing" = offline bag-of-words embedder (no API calls).
    embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768
    embedding_location: str = "us-central1"
    vector_store: str = "firestore"  # firestore | local
    firestore_database: str = "(default)"
    firestore_collection: str = "job_description_chunks"
    local_vector_path: str = ".rag_store.json"
    # Model for grounded answers; empty means AGENT_MODEL.
    rag_answer_model: str = ""

    # Gmail/Sheets OAuth files (relative paths resolve against the repo root).
    token_path: str = "token.json"
    credentials_path: str = "credentials.json"

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def llm_enabled(self) -> bool:
        return self.use_llm and bool(self.gcp_project)


def _coerce(value: Any, target_type: Any) -> Any:
    if target_type in (bool, "bool"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if target_type in (int, "int"):
        return int(value)
    return str(value)


def load_settings(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    """Load settings: defaults < config.json < environment < explicit overrides."""
    env = os.environ if env is None else env
    cfg_path = Path(path) if path else Path(env.get("TRACKER_CONFIG", DEFAULT_CONFIG_PATH))
    file_cfg: dict[str, Any] = {}
    if cfg_path.exists():
        file_cfg = json.loads(cfg_path.read_text())

    values: dict[str, Any] = {}
    for f in fields(Settings):
        key = f.name.upper()
        if key in env and env[key] != "":
            values[f.name] = _coerce(env[key], f.type)
        elif key in file_cfg:
            values[f.name] = _coerce(file_cfg[key], f.type)

    # Standard Google env var as a fallback for the project id.
    if not values.get("gcp_project") and env.get("GOOGLE_CLOUD_PROJECT"):
        values["gcp_project"] = env["GOOGLE_CLOUD_PROJECT"]

    settings = Settings(**values)
    return replace(settings, **overrides) if overrides else settings
