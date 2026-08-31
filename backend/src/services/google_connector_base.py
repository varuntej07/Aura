"""
Shared credential layer for Google API connectors (Calendar, Gmail).

Owns the Firestore-backed OAuth credential storage at
users/{uid}/integrations/{doc_id}: loading credentials, persisting refreshed
tokens (preserving an existing refresh token when Google omits one), building
an authenticated API client, and classifying revocation errors. Subclasses
parameterize the provider doc id, scopes, and API name/version, and keep
their own provider-specific lifecycle logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore as fs
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..config.settings import settings
from .firebase import admin_firestore


class ReauthorizationRequired(Exception):
    """Stored Google credentials can no longer authorize API access."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class GoogleConnectorBase:
    """Credential storage and client construction shared by Google connectors."""

    CONNECTOR_DOC_ID: ClassVar[str]
    SCOPES: ClassVar[list[str]]
    SCOPE_STRING: ClassVar[str]
    API_NAME: ClassVar[str]
    API_VERSION: ClassVar[str]
    NOT_CONNECTED_ERROR: ClassVar[str]
    EXPIRED_ERROR: ClassVar[str]
    REAUTH_MESSAGE: ClassVar[str]

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _user_ref(self) -> fs.DocumentReference:
        return self._db().collection("users").document(self._user_id)

    def _integration_ref(self) -> fs.DocumentReference:
        return self._user_ref().collection("integrations").document(self.CONNECTOR_DOC_ID)

    def _load_integration(self) -> dict[str, Any]:
        doc = self._integration_ref().get()
        return doc.to_dict() or {}

    def _credentials_from_integration(self) -> Credentials | None:
        data = self._load_integration()
        refresh_token = data.get("refresh_token")
        access_token = data.get("access_token")
        if not refresh_token and not access_token:
            return None

        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=list(self.SCOPES),
        )
        expiry = _parse_iso(data.get("expiry_at"))
        if expiry is not None:
            creds.expiry = expiry.replace(tzinfo=None)  # google-auth compares against naive utcnow()
        return creds

    def _persist_credentials(
        self,
        *,
        access_token: str | None,
        refresh_token: str | None,
        expiry_at: datetime | None,
        enabled: bool = True,
        last_error: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        existing = self._load_integration()
        payload: dict[str, Any] = {
            "provider": self.CONNECTOR_DOC_ID,
            "enabled": enabled,
            "scope": self.SCOPE_STRING,
            "updated_at": _to_iso(now),
            "last_error": last_error,
        }
        if access_token:
            payload["access_token"] = access_token
        if refresh_token:
            payload["refresh_token"] = refresh_token
        elif existing.get("refresh_token"):
            payload["refresh_token"] = existing.get("refresh_token")
        if expiry_at:
            payload["expiry_at"] = _to_iso(expiry_at)
        if extra_fields:
            payload.update(extra_fields)
        if not existing:
            payload["connected_at"] = _to_iso(now)

        self._integration_ref().set(payload, merge=True)

    def _build_api_client(self, refresh: bool = True) -> Any:
        integration = self._load_integration()
        creds = self._credentials_from_integration()
        if creds is None:
            raise ValueError(self.NOT_CONNECTED_ERROR)

        if refresh and (not creds.valid or creds.expired):
            if not creds.refresh_token:
                raise ValueError(self.EXPIRED_ERROR)
            creds.refresh(GoogleAuthRequest())
            self._persist_credentials(
                access_token=creds.token,
                refresh_token=creds.refresh_token,
                expiry_at=creds.expiry,
                enabled=bool(integration.get("enabled")),
            )

        return build(self.API_NAME, self.API_VERSION, credentials=creds, cache_discovery=False)

    @staticmethod
    def _requires_reauthorization(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "invalid_grant",
                "token has been expired or revoked",
                "token_revoked",
                "reconnect required",
                "not connected",
            )
        )

    def _mark_reauthorization_required(self) -> None:
        self._integration_ref().set(
            {
                "enabled": False,
                "last_error": self.REAUTH_MESSAGE,
                "updated_at": _to_iso(_utc_now()),
            },
            merge=True,
        )

    def _write_enabled_state(self, enabled: bool) -> None:
        self._integration_ref().set(
            {
                "enabled": enabled,
                "last_error": None,
                "updated_at": _to_iso(_utc_now()),
            },
            merge=True,
        )
