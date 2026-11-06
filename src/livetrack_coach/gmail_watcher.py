"""Find the LiveTrack share link in the Garmin autostart email via the Gmail API.

One-time setup: `livetrack-coach gmail-auth` runs the OAuth consent for the
gmail.readonly scope and stores the refresh token under the data dir. After
that `livetrack-coach run --gmail` polls users.messages.list for mail from
garmin.com newer than when the watcher started, and returns the first
message that contains a livetrack.garmin.com/session/... link."""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .livetrack import SessionRef, extract_session_ref

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Gmail support needs: uv sync --extra gmail") from exc
    return Request, Credentials, InstalledAppFlow, build


def authorize(settings: Settings, open_browser: bool = True) -> Path:
    """Run the installed-app OAuth flow once and store the token file."""
    Request, Credentials, InstalledAppFlow, _ = _google()
    if not settings.gmail_client_json:
        raise SystemExit("GMAIL_OAUTH_CLIENT_JSON is not set (contents of the OAuth Desktop client JSON)")
    client_config = json.loads(settings.gmail_client_json)
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser)
    settings.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.gmail_token_path.write_text(creds.to_json())
    return settings.gmail_token_path


def _service(settings: Settings):
    Request, Credentials, _, build = _google()
    if not settings.gmail_token_path.is_file():
        raise SystemExit("no Gmail token; run `livetrack-coach gmail-auth` first")
    creds = Credentials.from_authorized_user_file(str(settings.gmail_token_path), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        settings.gmail_token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _walk_parts(payload: dict[str, Any]):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def message_text(msg: dict[str, Any]) -> str:
    """Concatenate every decoded text/* body part of a users.messages.get(format=full) result."""
    chunks = []
    for part in _walk_parts(msg.get("payload", {})):
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and str(part.get("mimeType", "")).startswith("text/"):
            chunks.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace"))
    if not chunks and msg.get("snippet"):
        chunks.append(msg["snippet"])
    return "\n".join(chunks)


def find_session_in_message(msg: dict[str, Any]) -> SessionRef | None:
    return extract_session_ref(message_text(msg))


class GmailWatcher:
    def __init__(self, settings: Settings, since_epoch: float | None = None):
        self.settings = settings
        self.since_epoch = int(since_epoch if since_epoch is not None else time.time()) - 120
        self.seen: set[str] = set()
        self._svc = None
        self._load_state()

    def _load_state(self) -> None:
        p = self.settings.gmail_state_path
        if p.is_file():
            try:
                self.seen = set(json.loads(p.read_text()).get("seen", []))
            except ValueError:
                self.seen = set()

    def _save_state(self) -> None:
        p = self.settings.gmail_state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"seen": sorted(self.seen)[-500:]}))

    @property
    def svc(self):
        if self._svc is None:
            self._svc = _service(self.settings)
        return self._svc

    def query(self) -> str:
        return f"from:{self.settings.gmail_sender} after:{self.since_epoch}"

    def check_once(self) -> SessionRef | None:
        resp = self.svc.users().messages().list(userId="me", q=self.query(), maxResults=10).execute()
        for item in resp.get("messages", []) or []:
            mid = item["id"]
            if mid in self.seen:
                continue
            msg = self.svc.users().messages().get(userId="me", id=mid, format="full").execute()
            self.seen.add(mid)
            self._save_state()
            ref = find_session_in_message(msg)
            if ref:
                log.info("LiveTrack link found in Gmail message %s", mid)
                return ref
        return None

    def wait_for_session(self, sleep=time.sleep) -> SessionRef:
        log.info("watching Gmail (%s) every %.0fs", self.query(), self.settings.gmail_poll_interval_s)
        while True:
            try:
                ref = self.check_once()
            except Exception as exc:  # noqa: BLE001 - keep watching through transient API errors
                log.warning("gmail poll failed: %s", exc)
                ref = None
            if ref:
                return ref
            sleep(self.settings.gmail_poll_interval_s)
