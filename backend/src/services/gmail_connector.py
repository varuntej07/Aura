"""
Gmail connector lifecycle and message read/send.

Mirrors the credential storage and refresh shape of GoogleCalendarConnector.
Credentials live at users/{uid}/integrations/gmail. Reads are on-demand (no
webhook sync) and surfaced to Buddy through the list_emails / read_email /
send_email tools.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore as fs
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..config.settings import settings
from ..lib.logger import logger
from .firebase import admin_firestore
from .google_oauth import exchange_server_auth_code

# Read + send. messages.send requires gmail.send (gmail.modify does NOT grant
# sending). gmail.readonly covers list/get. Both are Google "restricted" scopes,
# so the OAuth app must be verified (or the account added as a test user) before use.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPES = [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE]

CONNECTOR_DOC_ID = "gmail"


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


def _header(headers: list[dict[str, Any]], name: str) -> str | None:
    target = name.lower()
    for entry in headers or []:
        if str(entry.get("name", "")).lower() == target:
            return entry.get("value")
    return None


def _extract_plain_text(payload: dict[str, Any]) -> str:
    """Walk a Gmail message payload and return the best-effort plain-text body."""

    def _decode(data: str | None) -> str:
        if not data:
            return ""
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    mime_type = payload.get("mimeType", "")
    body_data = (payload.get("body") or {}).get("data")

    if mime_type == "text/plain" and body_data:
        return _decode(body_data)

    parts = payload.get("parts") or []
    # Prefer a text/plain part; fall back to the first part with decodable data.
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _extract_plain_text(part)
            if text:
                return text
    for part in parts:
        text = _extract_plain_text(part)
        if text:
            return text

    if body_data:
        return _decode(body_data)
    return ""


class GmailConnector:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _integration_ref(self) -> fs.DocumentReference:
        return (
            self._db()
            .collection("users")
            .document(self._user_id)
            .collection("integrations")
            .document(CONNECTOR_DOC_ID)
        )

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
            scopes=GMAIL_SCOPES,
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
        email_address: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = _utc_now()
        existing = self._load_integration()
        payload: dict[str, Any] = {
            "provider": CONNECTOR_DOC_ID,
            "enabled": enabled,
            "scope": " ".join(GMAIL_SCOPES),
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
        if email_address:
            payload["email_address"] = email_address
        if not existing:
            payload["connected_at"] = _to_iso(now)

        self._integration_ref().set(payload, merge=True)

    def _gmail_client(self, refresh: bool = True) -> Any:
        creds = self._credentials_from_integration()
        if creds is None:
            raise ValueError("Gmail is not connected.")

        if refresh and (not creds.valid or creds.expired):
            if not creds.refresh_token:
                raise ValueError("Gmail connection has expired. Reconnect required.")
            creds.refresh(GoogleAuthRequest())
            self._persist_credentials(
                access_token=creds.token,
                refresh_token=creds.refresh_token,
                expiry_at=creds.expiry,
            )

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # Lifecycle

    def get_status(self) -> dict[str, Any]:
        integration = self._load_integration()
        return {
            "enabled": bool(integration.get("enabled")),
            "email_address": integration.get("email_address"),
            "connected_at": integration.get("connected_at"),
            "last_error": integration.get("last_error"),
        }

    def connect(self, auth_code: str) -> dict[str, Any]:
        token_data = exchange_server_auth_code(auth_code)
        expires_in = int(token_data.get("expires_in", 3600) or 3600)
        expiry_at = _utc_now() + timedelta(seconds=expires_in)

        self._persist_credentials(
            access_token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            expiry_at=expiry_at,
            enabled=True,
            last_error=None,
        )

        # Best-effort: record which mailbox is connected for display.
        try:
            service = self._gmail_client(refresh=True)
            profile = service.users().getProfile(userId="me").execute()
            self._persist_credentials(
                access_token=None,
                refresh_token=None,
                expiry_at=None,
                email_address=profile.get("emailAddress"),
            )
        except Exception as exc:
            logger.warn("Gmail profile lookup failed after connect", {
                "user_id": self._user_id,
                "error": str(exc),
            })

        return self.get_status()

    def disconnect(self) -> dict[str, Any]:
        self._integration_ref().delete()
        return self.get_status()

    # Read / send

    def list_recent_messages(self, *, query: str | None = None, limit: int = 10) -> dict[str, Any]:
        integration = self._load_integration()
        if not integration.get("enabled"):
            return {"configured": False, "messages": []}

        service = self._gmail_client(refresh=True)
        limit = max(1, min(limit, 25))
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query or "", maxResults=limit)
            .execute()
        )

        messages: list[dict[str, Any]] = []
        for ref in listing.get("messages", []) or []:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = (msg.get("payload") or {}).get("headers", [])
            messages.append({
                "id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "from": _header(headers, "From"),
                "subject": _header(headers, "Subject"),
                "date": _header(headers, "Date"),
                "snippet": msg.get("snippet"),
                "unread": "UNREAD" in (msg.get("labelIds") or []),
            })

        return {"configured": True, "messages": messages}

    def get_message(self, *, message_id: str) -> dict[str, Any]:
        integration = self._load_integration()
        if not integration.get("enabled"):
            return {"configured": False}

        service = self._gmail_client(refresh=True)
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = msg.get("payload") or {}
        headers = payload.get("headers", [])
        return {
            "configured": True,
            "id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "snippet": msg.get("snippet"),
            "body": _extract_plain_text(payload),
        }

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        integration = self._load_integration()
        if not integration.get("enabled"):
            raise ValueError("Gmail is not connected.")
        if not to.strip():
            raise ValueError("Recipient (to) is required.")

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject or ""
        sender = integration.get("email_address")
        if sender:
            message["From"] = sender
        message.set_content(body or "")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service = self._gmail_client(refresh=True)
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return {
            "sent": True,
            "id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "to": to,
            "subject": subject,
        }
