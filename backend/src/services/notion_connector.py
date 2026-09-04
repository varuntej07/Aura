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
import random
import threading
import time
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

# Retry policy for one authorized call, per Notion's own guidance
# (developers.notion.com/reference/request-limits, checked 2026-09-04): respect
# Retry-After on 429, exponential backoff with jitter, small attempt cap. The
# cap here is deliberately below Notion's suggested ~6 because callers sit
# behind their own outer retry layers (voice worker timeouts, the research
# engine's stage attempt cap, Cloud Tasks) and each layer multiplies attempts.
_MAX_SEND_ATTEMPTS = 3
_RETRY_STATUSES_IDEMPOTENT = frozenset({429, 500, 502, 503, 504})
_RETRY_STATUSES_ALWAYS = frozenset({429})
_BACKOFF_BASE_S = 0.5
_MAX_RETRY_AFTER_S = 5.0

# One refresh in flight per uid, process-wide. Notion allows a single refresh
# per connection at a time and rotates the pair. NOTE this lock is honest only
# within one process: Cloud Run runs many instances, so the cross-instance
# race is closed by the transactional compare-and-swap in _refresh(), not by
# this lock. The lock remains as the cheap in-process fast path.
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS_MAX = 512


class NotionReauthorizationRequired(ReauthorizationRequired):
    """Stored Notion credentials can no longer authorize API access."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _refresh_lock(uid: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(uid)
        if lock is None:
            if len(_REFRESH_LOCKS) >= _REFRESH_LOCKS_MAX:
                # Bounded: drop idle locks so a long-lived instance serving many
                # users never grows this dict without limit. A lock mid-refresh
                # is never evicted.
                for key in [k for k, v in _REFRESH_LOCKS.items() if not v.locked()]:
                    _REFRESH_LOCKS.pop(key, None)
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

    def _persist_token_pair_if_current(
        self,
        token_data: dict[str, Any],
        *,
        expected_refresh_token: str,
        enabled: bool,
    ) -> bool:
        """Transactional compare-and-swap for a refresh rotation.

        Writes the rotated pair only if the stored refresh_token is still the
        one this rotation consumed; returns False when another instance's
        rotation landed first (the caller then serves that instance's pair).
        """
        db = admin_firestore()
        ref = self._integration_ref()
        transaction = db.transaction()

        @fs.transactional
        def _swap(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            current = snap.to_dict() or {}
            if str(current.get("refresh_token") or "") != expected_refresh_token:
                return False
            txn.set(
                ref,
                {
                    "provider": CONNECTOR_DOC_ID,
                    "enabled": enabled,
                    "access_token": token_data.get("access_token"),
                    "refresh_token": token_data.get("refresh_token"),
                    "bot_id": token_data.get("bot_id"),
                    "workspace_id": token_data.get("workspace_id"),
                    "workspace_name": token_data.get("workspace_name"),
                    "updated_at": _utc_now_iso(),
                    "last_error": None,
                },
                merge=True,
            )
            return True

        return bool(_swap(transaction))

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
                # Non-JSON error body; the status code still tells the story.
                logger.warn(
                    "notion_connector: token error body was not JSON",
                    {"user_id": self._user_id, "status": response.status_code},
                )
            raise ValueError(
                f"Notion token request failed ({response.status_code}): {error_code or 'unknown_error'}"
            )
        return response.json()

    def _refresh(self, *, stale_access_token: str = "") -> str:
        """Rotate the token pair; return the new access token.

        Two-layer race protection, because refresh tokens ROTATE and a lost
        race that replays the old refresh token bricks the connector:

        1. In-process: single-flight per uid via _refresh_lock; the loser
           re-reads the pair the winner persisted.
        2. Cross-instance: if the stored access token already differs from the
           one that 401'd, another Cloud Run instance rotated the pair between
           our 401 and this call - use the stored token and skip the refresh
           POST entirely. The persist itself is a transactional compare-and-swap
           on refresh_token, so two instances that both reach the token
           endpoint cannot interleave their writes (the loser's write is
           dropped and it re-reads the winner's pair).

        A residual window remains where two instances both POST the refresh
        before either persists; Notion rejects the second with invalid_grant
        and that path marks reauthorization required. That window cannot be
        closed without a distributed lock and is accepted.
        """
        with _refresh_lock(self._user_id):
            integration = self._load_integration()
            stored_access = str(integration.get("access_token") or "")
            if stale_access_token and stored_access and stored_access != stale_access_token:
                # Someone else already rotated; their pair is the live one.
                return stored_access
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
            access_token = token_data.get("access_token")
            if not access_token:
                self._mark_reauthorization_required()
                raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
            persisted = self._persist_token_pair_if_current(
                token_data,
                expected_refresh_token=str(refresh_token),
                enabled=bool(integration.get("enabled", True)),
            )
            if not persisted:
                # A concurrent rotation on another instance won the swap; its
                # persisted pair supersedes ours. Serve whatever it stored.
                current = self._load_integration()
                current_access = str(current.get("access_token") or "")
                if current_access:
                    return current_access
            return str(access_token)

    # Authorized REST access

    def authorized_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        require_enabled: bool = True,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """Call the Notion API for this user, refreshing reactively on 401.

        Owns ALL of the per-request resilience so no caller re-implements it:
        429s are retried honoring Retry-After, transient 5xx are retried for
        idempotent requests, both with exponential backoff and full jitter, and
        a 401 triggers one refresh-and-replay. Returns the (possibly non-2xx)
        response for the caller to interpret; only credential and rate-limit
        outcomes are decided here. Raises NotionReauthorizationRequired when
        the stored credentials are dead.

        idempotent: whether a duplicate send is harmless. Defaults to True for
        GET, False otherwise. Callers whose writes are protected by their own
        receipt (page create behind the notion_writes receipt) may pass True
        explicitly; unguarded creates (database create) must not.
        require_enabled=False is for enable() itself, which must prove the
        credentials before flipping the flag.
        """
        integration = self._load_integration()
        if require_enabled and not integration.get("enabled"):
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        access_token = integration.get("access_token")
        if not access_token:
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        if idempotent is None:
            idempotent = method.upper() == "GET"

        response = self._send_with_retries(
            method, path, access_token=str(access_token), json_body=json_body,
            idempotent=idempotent,
        )
        if response.status_code != 401:
            return response

        # Reactive refresh: no expires_in exists to schedule against.
        access_token = self._refresh(stale_access_token=str(access_token))
        response = self._send_with_retries(
            method, path, access_token=str(access_token), json_body=json_body,
            idempotent=idempotent,
        )
        if response.status_code == 401:
            self._mark_reauthorization_required()
            raise NotionReauthorizationRequired(self.REAUTH_MESSAGE)
        return response

    def _send_with_retries(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json_body: dict[str, Any] | None,
        idempotent: bool,
    ) -> httpx.Response:
        """One logical send with the Notion-recommended retry discipline.

        429 retries always (the request never executed); transient 5xx retries
        only when a duplicate would be harmless. Sleeps are synchronous by
        design: every caller already runs this under asyncio.to_thread, and the
        caller's own HTTP timeout is the outer bound that cuts a slow retry
        chain short.
        """
        retryable = (
            _RETRY_STATUSES_IDEMPOTENT if idempotent else _RETRY_STATUSES_ALWAYS
        )
        response: httpx.Response | None = None
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            response = self._send(
                method, path, access_token=access_token, json_body=json_body
            )
            status = response.status_code
            if status not in retryable or attempt == _MAX_SEND_ATTEMPTS:
                return response
            if status == 429:
                try:
                    delay = float(response.headers.get("Retry-After") or 1.0)
                except ValueError:
                    delay = 1.0
                delay = min(max(delay, 0.0), _MAX_RETRY_AFTER_S)
            else:
                # Full jitter: uniform over the exponentially-grown window.
                delay = random.uniform(0.0, _BACKOFF_BASE_S * (2 ** (attempt - 1)))
            logger.warn(
                "notion_connector: retrying request",
                {
                    "user_id": self._user_id,
                    "path": path,
                    "status": status,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                },
            )
            time.sleep(delay)
        assert response is not None  # loop always executes at least once
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
