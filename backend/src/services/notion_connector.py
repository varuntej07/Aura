"""
Notion connector lifecycle and authorized REST access.

Standalone by design: GoogleConnectorBase is Google-shaped (Google token
endpoint, googleapiclient clients), so this class reuses only its Firestore
credential layout (users/{uid}/integrations/notion) and the shared
ReauthorizationRequired contract, not the class itself.

Token model, verified against live developers.notion.com on 2026-09-03:
access tokens EXPIRE and refresh tokens ROTATE, but the code-exchange
response carries no expires_in, so refresh is reactive. On a 401 the
connector refreshes once (single-flight per uid) and replays the request
once; a failed refresh, or a 401 with no stored refresh token, disables the
integration and raises NotionReauthorizationRequired, which the handlers map
to the existing 409 reauthorization_required contract. Every successful OAuth
authorization mints a fresh token pair, so connect() overwrites the stored
pair unconditionally.
"""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
from google.cloud import firestore as fs

from ..config.settings import settings
from ..lib.logger import logger
from .firebase import admin_firestore
from .google_connector_base import ReauthorizationRequired

CONNECTOR_DOC_ID = "notion"
NOTION_API_BASE = "https://api.notion.com"
NOTION_TOKEN_ENDPOINT = f"{NOTION_API_BASE}/v1/oauth/token"
# Pinned API version (developers.notion.com/reference/versioning, checked
# 2026-09-03). 2025-09-03+ made data sources the unit of search, schema, and
# page parenting; resolve/schema/write in services/notion/ depend on that.
NOTION_VERSION = "2026-03-11"
_REQUEST_TIMEOUT_S = 10.0

# One refresh in flight per uid, process-wide. Notion allows a single refresh
# per connection at a time and rotates the pair; a lost race that persists the
# stale pair bricks the connector until reconnect.
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


class NotionReauthorizationRequired(ReauthorizationRequired):
    """Stored Notion credentials can no longer authorize API access."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _refresh_lock(uid: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(uid)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[uid] = lock
        return lock


def _basic_auth_header() -> str:
    raw = f"{settings.NOTION_CLIENT_ID}:{settings.NOTION_CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class NotionConnector:
    REAUTH_MESSAGE = "Notion authorization is required."

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    # Firestore credential layout (mirrors GoogleConnectorBase's shape)

    def _integration_ref(self) -> fs.DocumentReference:
        return (
            admin_firestore()
            .collection("users")
            .document(self._user_id)
            .collection("integrations")
            .document(CONNECTOR_DOC_ID)
        )

    def _load_integration(self) -> dict[str, Any]:
        doc = self._integration_ref().get()
        return doc.to_dict() or {}

    def _persist_token_pair(
        self,
        token_data: dict[str, Any],
        *,
        enabled: bool = True,
    ) -> None:
        """Persist a full exchange/refresh response, overwriting the old pair.

        One merge write so the rotated access/refresh tokens land atomically;
        never preserve the previous refresh token, a rotation retired it.
        """
        now = _utc_now_iso()
        existing = self._load_integration()
        payload: dict[str, Any] = {
            "provider": CONNECTOR_DOC_ID,
            "enabled": enabled,
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "bot_id": token_data.get("bot_id"),
            "workspace_id": token_data.get("workspace_id"),
            "workspace_name": token_data.get("workspace_name"),
            "updated_at": now,
            "last_error": None,
        }
        if not existing:
            payload["connected_at"] = now
        self._integration_ref().set(payload, merge=True)

    def _mark_reauthorization_required(self) -> None:
        self._integration_ref().set(
            {
                "enabled": False,
                "last_error": self.REAUTH_MESSAGE,
                "updated_at": _utc_now_iso(),
            },
            merge=True,
        )

    def _write_enabled_state(self, enabled: bool) -> None:
        self._integration_ref().set(
            {
                "enabled": enabled,
                "last_error": None,
                "updated_at": _utc_now_iso(),
            },
            merge=True,
        )

    # OAuth

    def connect(self, auth_code: str, *, redirect_uri: str) -> dict[str, Any]:
        token_data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
            }
        )
        self._persist_token_pair(token_data, enabled=True)
        return self.get_status()

    def _token_request(self, body: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
            response = client.post(
                NOTION_TOKEN_ENDPOINT,
                json=body,
                headers={"Authorization": _basic_auth_header()},
            )
        if response.status_code != 200:
            error_code = ""
            try:
                error_code = str(response.json().get("error") or "")
            except Exception:
                pass
            raise ValueError(
                f"Notion token request failed ({response.status_code}): {error_code or 'unknown_error'}"
            )
        return response.json()

    def _refresh(self) -> str:
        """Rotate the token pair; return the new access token.

        Single-flight per uid: a concurrent caller that lost the race re-reads
        the pair the winner persisted instead of burning the rotated refresh
        token a second time.
        """
        with _refresh_lock(self._user_id):
            integration = self._load_integration()
            refresh_token = integration.get("refresh_token")
            if not refresh_token:
                self._mark_reauthorization_required()
                raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
            try:
                token_data = self._token_request(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    }
                )
            except ValueError as exc:
                # invalid_grant = revoked or already-rotated-away; either way the
                # stored pair is dead and only the user can mint a new one.
                logger.warn(
                    "notion_connector: token refresh failed",
                    {"user_id": self._user_id, "error": str(exc)},
                )
                self._mark_reauthorization_required()
                raise NotionReauthorizationRequired(self.REAUTH_MESSAGE) from exc
            self._persist_token_pair(
                token_data,
                enabled=bool(integration.get("enabled", True)),
            )
            access_token = token_data.get("access_token")
            if not access_token:
                self._mark_reauthorization_required()
                raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
            return str(access_token)

    # Authorized REST access

    def authorized_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        require_enabled: bool = True,
    ) -> httpx.Response:
        """Call the Notion API for this user, refreshing reactively on 401.

        Returns the (possibly non-2xx) response for the caller to interpret;
        only the credential outcomes are decided here. Raises
        NotionReauthorizationRequired when the stored credentials are dead.
        require_enabled=False is for enable() itself, which must prove the
        credentials before flipping the flag.
        """
        integration = self._load_integration()
        if require_enabled and not integration.get("enabled"):
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        access_token = integration.get("access_token")
        if not access_token:
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)

        response = self._send(method, path, access_token=str(access_token), json_body=json_body)
        if response.status_code != 401:
            return response

        # Reactive refresh: no expires_in exists to schedule against.
        access_token = self._refresh()
        response = self._send(method, path, access_token=access_token, json_body=json_body)
        if response.status_code == 401:
            self._mark_reauthorization_required()
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        return response

    @staticmethod
    def _send(
        method: str,
        path: str,
        *,
        access_token: str,
        json_body: dict[str, Any] | None,
    ) -> httpx.Response:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
            return client.request(
                method,
                f"{NOTION_API_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Notion-Version": NOTION_VERSION,
                },
                json=json_body,
            )

    # Lifecycle

    def get_status(self) -> dict[str, Any]:
        integration = self._load_integration()
        return {
            "enabled": bool(integration.get("enabled")),
            "can_reconnect": bool(
                integration.get("refresh_token") or integration.get("access_token")
            ),
            "workspace_name": integration.get("workspace_name"),
            "connected_at": integration.get("connected_at"),
            "last_error": integration.get("last_error"),
        }

    def enable(self) -> dict[str, Any]:
        integration = self._load_integration()
        if not integration.get("refresh_token") and not integration.get("access_token"):
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        # Prove the credentials still authorize before flipping the flag; a
        # bot the user removed workspace-side answers 401 to everything.
        response = self.authorized_request("GET", "/v1/users/me", require_enabled=False)
        if response.status_code == 401:
            self._mark_reauthorization_required()
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        self._write_enabled_state(True)
        return self.get_status()

    def disable(self) -> dict[str, Any]:
        self._write_enabled_state(False)
        return self.get_status()

    def disconnect(self) -> dict[str, Any]:
        self._integration_ref().delete()
        return self.get_status()
