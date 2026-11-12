"""Credentials for Gmail + Sheets (user OAuth) and service construction.

Vertex AI uses Application Default Credentials instead (gcloud locally, the
service account on Cloud Run), so it is not handled here.

Token sources, in priority order:
1. ``GOOGLE_OAUTH_TOKEN_JSON`` env var (GitHub Actions secret).
2. The file at ``settings.token_path`` (local ``token.json`` or a Secret
   Manager volume mounted on Cloud Run).
3. Interactive browser consent using ``credentials.json`` (local only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


class AuthError(RuntimeError):
    pass


def load_user_credentials(settings: Settings, interactive: bool = False) -> Credentials:
    creds: Credentials | None = None
    token_file: Path | None = None

    env_token = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
    if env_token:
        creds = Credentials.from_authorized_user_info(json.loads(env_token), SCOPES)
    else:
        token_file = settings.resolve(settings.token_path)
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            _try_save(creds, token_file)
            return creds
        except Exception as exc:  # expired/revoked refresh token
            if not interactive:
                raise AuthError(
                    f"Gmail OAuth token could not be refreshed ({exc}). "
                    "Run `python main.py --reauth` locally to create a new token.json."
                ) from exc

    if not interactive:
        raise AuthError("No valid Gmail OAuth token found. Run `python main.py --reauth` locally first.")
    return run_consent_flow(settings)


def run_consent_flow(settings: Settings) -> Credentials:
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = settings.resolve(settings.credentials_path)
    if not client_file.exists():
        raise AuthError(f"OAuth client file not found: {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    _try_save(creds, settings.resolve(settings.token_path))
    return creds


def _try_save(creds: Credentials, token_file: Path | None) -> None:
    """Persist refreshed tokens when the token came from a writable file."""
    if token_file is None:
        return
    try:
        token_file.write_text(creds.to_json())
    except OSError:
        pass  # read-only secret mount on Cloud Run; the refresh token stays valid


def build_services(creds: Credentials):
    from googleapiclient.discovery import build

    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return gmail, sheets
