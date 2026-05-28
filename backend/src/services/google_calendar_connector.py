"""
Google Calendar connector lifecycle, webhook ingestion, and cached event sync.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore as fs
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..config.settings import settings
from ..lib.logger import logger
from .firebase import admin_firestore

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CHANNELS_COLLECTION = "google_calendar_channels"
SYNC_JOBS_COLLECTION = "google_calendar_sync_jobs"
CONNECTOR_DOC_ID = "google_calendar"
SOURCE_DOC_ID = "primary"


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


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        return _parse_iso(value)
    return None


def _safe_doc_id(calendar_id: str, event_id: str) -> str:
    safe_calendar = calendar_id.replace("/", "_")
    return f"{safe_calendar}__{event_id}"


def _event_range_to_utc(
    raw: dict[str, Any] | None,
    default_tz: str,
) -> tuple[datetime | None, bool, str | None]:
    if not raw:
        return None, False, None

    if raw.get("dateTime"):
        dt = datetime.fromisoformat(str(raw["dateTime"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC), False, raw.get("timeZone")

    if raw.get("date"):
        tz = ZoneInfo(raw.get("timeZone") or default_tz or "UTC")
        dt = datetime.fromisoformat(str(raw["date"])).replace(tzinfo=tz)
        return dt.astimezone(UTC), True, str(tz)

    return None, False, None


class GoogleCalendarConnector:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def _db(self) -> fs.Client:
        return admin_firestore()

    def _user_ref(self) -> fs.DocumentReference:
        return self._db().collection("users").document(self._user_id)

    def _integration_ref(self) -> fs.DocumentReference:
        return self._user_ref().collection("integrations").document(CONNECTOR_DOC_ID)

    def _source_ref(self, calendar_id: str = SOURCE_DOC_ID) -> fs.DocumentReference:
        return self._user_ref().collection("calendar_sources").document(calendar_id)

    def _events_ref(self) -> fs.CollectionReference:
        return self._user_ref().collection("calendar_events")

    def _channel_ref(self, channel_id: str) -> fs.DocumentReference:
        return self._db().collection(CHANNELS_COLLECTION).document(channel_id)

    def _job_ref(self, calendar_id: str = SOURCE_DOC_ID) -> fs.DocumentReference:
        job_id = f"{self._user_id}__{calendar_id.replace('/', '_')}"
        return self._db().collection(SYNC_JOBS_COLLECTION).document(job_id)

    def _load_integration(self) -> dict[str, Any]:
        doc = self._integration_ref().get()
        return doc.to_dict() or {}

    def _load_source(self, calendar_id: str = SOURCE_DOC_ID) -> dict[str, Any]:
        doc = self._source_ref(calendar_id).get()
        return doc.to_dict() or {}

    def _exchange_server_auth_code(self, auth_code: str) -> dict[str, Any]:
        form_fields: dict[str, str] = {
            "code": auth_code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "grant_type": "authorization_code",
        }
        if settings.GOOGLE_REDIRECT_URI:
            form_fields["redirect_uri"] = settings.GOOGLE_REDIRECT_URI
        form = urllib.parse.urlencode(form_fields).encode("utf-8")

        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("Google OAuth code exchange failed", {
                "user_id": self._user_id,
                "status": exc.code,
                "body": body[:300],
            })
            raise ValueError(f"Google token exchange failed ({exc.code}): {body[:200]}") from exc
        except Exception as exc:
            logger.exception("Google OAuth code exchange failed", {
                "user_id": self._user_id,
                "error": str(exc),
            })
            raise

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
            scopes=[CALENDAR_SCOPE],
        )
        expiry = _parse_iso(data.get("expiry_at"))
        if expiry is not None:
            creds.expiry = expiry.replace(tzinfo=None)  # google-auth compares against datetime.utcnow() (naive)
        return creds

    def _persist_credentials(
        self,
        *,
        access_token: str | None,
        refresh_token: str | None,
        expiry_at: datetime | None,
        enabled: bool = True,
        last_error: str | None = None,
    ) -> None:
        now = _utc_now()
        existing = self._load_integration()
        payload: dict[str, Any] = {
            "provider": CONNECTOR_DOC_ID,
            "enabled": enabled,
            "scope": CALENDAR_SCOPE,
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
        if not existing:
            payload["connected_at"] = _to_iso(now)

        self._integration_ref().set(payload, merge=True)

    def _calendar_client(self, refresh: bool = True) -> Any:
        creds = self._credentials_from_integration()
        if creds is None:
            raise ValueError("Google Calendar is not connected.")

        if refresh and (not creds.valid or creds.expired):
            if not creds.refresh_token:
                raise ValueError("Google Calendar connection has expired. Reconnect required.")
            creds.refresh(GoogleAuthRequest())
            self._persist_credentials(
                access_token=creds.token,
                refresh_token=creds.refresh_token,
                expiry_at=creds.expiry,
            )

        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def calendar_client(self, refresh: bool = True) -> Any:
        return self._calendar_client(refresh=refresh)

    def get_status(self) -> dict[str, Any]:
        integration = self._load_integration()
        source = self._load_source()
        now = _utc_now()
        watch_expires_at = _parse_iso(source.get("watch_expires_at"))

        enabled = bool(integration.get("enabled"))
        watch_active = bool(
            source.get("channel_id") and watch_expires_at and watch_expires_at > now
        )
        automatic_sync_available = bool(
            settings.GOOGLE_CALENDAR_WEBHOOK_URL or watch_active
        )

        return {
            "enabled": enabled,
            "watch_active": watch_active,
            "automatic_sync_available": automatic_sync_available,
            "webhook_url_configured": bool(settings.GOOGLE_CALENDAR_WEBHOOK_URL),
            "calendar_id": source.get("calendar_id") or SOURCE_DOC_ID,
            "calendar_name": source.get("calendar_name") or "Primary",
            "calendar_time_zone": source.get("time_zone"),
            "connected_at": integration.get("connected_at"),
            "last_synced_at": source.get("last_synced_at"),
            "last_sync_status": source.get("last_sync_status"),
            "watch_expires_at": source.get("watch_expires_at"),
            "pending_sync": bool(source.get("pending_sync")),
            "last_error": source.get("last_error") or integration.get("last_error"),
        }

    def connect(self, auth_code: str, *, watch_url: str | None) -> dict[str, Any]:
        token_data = self._exchange_server_auth_code(auth_code)
        expires_in = int(token_data.get("expires_in", 3600) or 3600)
        expiry_at = _utc_now() + timedelta(seconds=expires_in)

        self._persist_credentials(
            access_token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            expiry_at=expiry_at,
            enabled=True,
            last_error=None,
        )

        self._sync_calendar(reason="initial_connect", force_full_sync=True)
        if watch_url:
            try:
                self._ensure_watch_channel(watch_url=watch_url)
            except Exception as exc:
                self._source_ref().set({
                    "last_error": f"Webhook watch setup failed: {exc}",
                    "watch_requested_url": watch_url,
                    "updated_at": _to_iso(_utc_now()),
                }, merge=True)
                logger.warn("Google Calendar watch setup failed", {
                    "user_id": self._user_id,
                    "error": str(exc),
                })

        return self.get_status()

    def disconnect(self) -> dict[str, Any]:
        source = self._load_source()
        channel_id = source.get("channel_id")
        resource_id = source.get("channel_resource_id")

        if channel_id and resource_id:
            try:
                self._stop_watch_channel(channel_id=channel_id, resource_id=resource_id)
            except Exception as exc:
                logger.warn("Failed to stop Google Calendar channel", {
                    "user_id": self._user_id,
                    "channel_id": channel_id,
                    "error": str(exc),
                })

        if channel_id:
            self._channel_ref(str(channel_id)).delete()

        self._job_ref().delete()
        self._purge_calendar_cache()
        self._source_ref().delete()
        self._integration_ref().delete()
        return self.get_status()

    def sync_now(self) -> dict[str, Any]:
        self._sync_calendar(reason="manual_resync")
        return self.get_status()

    def cache_api_events(self, events: list[dict[str, Any]]) -> None:
        source = self._load_source()
        self._persist_events(
            items=events,
            calendar_id=str(source.get("calendar_id") or SOURCE_DOC_ID),
            calendar_name=str(source.get("calendar_name") or "Primary"),
            calendar_time_zone=str(source.get("time_zone") or "UTC"),
        )

    def _purge_calendar_cache(self) -> None:
        batch = self._db().batch()
        op_count = 0
        for doc in self._events_ref().stream():
            batch.delete(doc.reference)
            op_count += 1
            if op_count == 400:
                batch.commit()
                batch = self._db().batch()
                op_count = 0
        if op_count > 0:
            batch.commit()

    def _persist_events(
        self,
        *,
        items: list[dict[str, Any]],
        calendar_id: str,
        calendar_name: str,
        calendar_time_zone: str,
    ) -> int:
        batch = self._db().batch()
        op_count = 0
        written = 0

        for event in items:
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue

            start_at, is_all_day, event_tz = _event_range_to_utc(
                event.get("start"),
                calendar_time_zone,
            )
            end_at, _, _ = _event_range_to_utc(
                event.get("end"),
                calendar_time_zone,
            )

            attendees = [
                {
                    "email": attendee.get("email"),
                    "response_status": attendee.get("responseStatus"),
                    "display_name": attendee.get("displayName"),
                }
                for attendee in event.get("attendees", []) or []
                if isinstance(attendee, dict)
            ]

            payload = {
                "calendar_id": calendar_id,
                "calendar_name": calendar_name,
                "provider_event_id": event_id,
                "summary": event.get("summary"),
                "description": event.get("description"),
                "location": event.get("location"),
                "status": event.get("status", "confirmed"),
                "html_link": event.get("htmlLink"),
                "hangout_link": event.get("hangoutLink"),
                "event_type": event.get("eventType"),
                "created_at_remote": event.get("created"),
                "updated_at_remote": event.get("updated"),
                "recurring_event_id": event.get("recurringEventId"),
                "organizer_email": (event.get("organizer") or {}).get("email"),
                "creator_email": (event.get("creator") or {}).get("email"),
                "attendees": attendees,
                "conference_data": event.get("conferenceData"),
                "time_zone": event_tz or calendar_time_zone,
                "is_all_day": is_all_day,
                "start_at": _to_iso(start_at),
                "end_at": _to_iso(end_at),
                "start_at_ts": start_at,
                "end_at_ts": end_at,
                "sync_updated_at": _to_iso(_utc_now()),
            }

            doc_ref = self._events_ref().document(_safe_doc_id(calendar_id, event_id))
            batch.set(doc_ref, payload, merge=True)
            op_count += 1
            written += 1

            if op_count == 400:
                batch.commit()
                batch = self._db().batch()
                op_count = 0

        if op_count > 0:
            batch.commit()

        return written

    def _sync_calendar(self, *, reason: str, force_full_sync: bool = False) -> None:
        source = self._load_source()
        calendar_id = str(source.get("calendar_id") or SOURCE_DOC_ID)
        sync_token = None if force_full_sync else source.get("sync_token")

        service = self._calendar_client(refresh=True)
        page_token: str | None = None
        next_sync_token: str | None = None
        total_written = 0
        calendar_time_zone = str(source.get("time_zone") or "UTC")
        calendar_name = str(source.get("calendar_name") or "Primary")

        while True:
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "singleEvents": True,
                "showDeleted": True,
                "maxResults": 250,
                "pageToken": page_token,
            }
            if sync_token:
                params["syncToken"] = sync_token

            try:
                response = service.events().list(**params).execute()
            except Exception as exc:
                if sync_token and "410" in str(exc):
                    logger.warn("Google Calendar sync token invalidated, forcing full sync", {
                        "user_id": self._user_id,
                        "calendar_id": calendar_id,
                    })
                    self._source_ref(calendar_id).set({
                        "sync_token": None,
                        "pending_sync": True,
                        "last_error": "Sync token invalidated. Rebuilding cache.",
                        "updated_at": _to_iso(_utc_now()),
                    }, merge=True)
                    self._sync_calendar(reason=f"{reason}_full_resync", force_full_sync=True)
                    return
                self._source_ref(calendar_id).set({
                    "pending_sync": False,
                    "last_sync_status": "error",
                    "last_error": str(exc),
                    "updated_at": _to_iso(_utc_now()),
                }, merge=True)
                raise

            calendar_time_zone = response.get("timeZone") or calendar_time_zone
            calendar_name = response.get("summary") or calendar_name
            total_written += self._persist_events(
                items=response.get("items", []) or [],
                calendar_id=calendar_id,
                calendar_name=calendar_name,
                calendar_time_zone=calendar_time_zone,
            )

            page_token = response.get("nextPageToken")
            next_sync_token = response.get("nextSyncToken") or next_sync_token
            if not page_token:
                break

        self._source_ref(calendar_id).set({
            "calendar_id": calendar_id,
            "calendar_name": calendar_name,
            "time_zone": calendar_time_zone,
            "sync_token": next_sync_token,
            "pending_sync": False,
            "last_sync_status": "ok",
            "last_error": None,
            "last_synced_at": _to_iso(_utc_now()),
            "last_sync_reason": reason,
            "last_sync_written_count": total_written,
            "updated_at": _to_iso(_utc_now()),
        }, merge=True)

    def _stop_watch_channel(self, *, channel_id: str, resource_id: str) -> None:
        service = self._calendar_client(refresh=True)
        service.channels().stop(body={
            "id": channel_id,
            "resourceId": resource_id,
        }).execute()

    def _ensure_watch_channel(self, *, watch_url: str) -> None:
        source = self._load_source()
        calendar_id = str(source.get("calendar_id") or SOURCE_DOC_ID)

        old_channel_id = source.get("channel_id")
        old_resource_id = source.get("channel_resource_id")
        if old_channel_id and old_resource_id:
            try:
                self._stop_watch_channel(
                    channel_id=str(old_channel_id),
                    resource_id=str(old_resource_id),
                )
            except Exception:
                logger.warn("Ignoring failure while replacing existing calendar watch channel", {
                    "user_id": self._user_id,
                    "calendar_id": calendar_id,
                    "channel_id": old_channel_id,
                })
            self._channel_ref(str(old_channel_id)).delete()

        service = self._calendar_client(refresh=True)
        channel_id = str(uuid4())
        channel_token = secrets.token_urlsafe(24)
        response = service.events().watch(
            calendarId=calendar_id,
            body={
                "id": channel_id,
                "token": channel_token,
                "type": "web_hook",
                "address": watch_url,
                "params": {"ttl": str(settings.GOOGLE_CALENDAR_WATCH_TTL_SECONDS)},
            },
        ).execute()

        expiration_ms = int(response.get("expiration", "0") or 0)
        expires_at = datetime.fromtimestamp(expiration_ms / 1000, tz=UTC) if expiration_ms else None

        self._channel_ref(channel_id).set({
            "user_id": self._user_id,
            "calendar_id": calendar_id,
            "resource_id": response.get("resourceId"),
            "token": channel_token,
            "watch_url": watch_url,
            "expires_at": _to_iso(expires_at),
            "created_at": _to_iso(_utc_now()),
        })
        self._source_ref(calendar_id).set({
            "channel_id": channel_id,
            "channel_resource_id": response.get("resourceId"),
            "channel_token": channel_token,
            "watch_expires_at": _to_iso(expires_at),
            "watch_requested_url": watch_url,
            "updated_at": _to_iso(_utc_now()),
            "last_error": None,
        }, merge=True)

    def enqueue_sync_from_notification(self, headers: dict[str, str]) -> dict[str, Any]:
        channel_id = headers.get("x-goog-channel-id", "")
        resource_id = headers.get("x-goog-resource-id", "")
        channel_token = headers.get("x-goog-channel-token", "")
        resource_state = headers.get("x-goog-resource-state", "")
        message_number = headers.get("x-goog-message-number", "")

        if not channel_id or not resource_id:
            raise ValueError("Missing Google Calendar channel headers.")

        channel_doc = self._channel_ref(channel_id).get()
        if not channel_doc.exists:
            raise ValueError("Unknown Google Calendar channel.")

        channel = channel_doc.to_dict() or {}
        if channel.get("resource_id") != resource_id:
            raise ValueError("Google Calendar resource ID mismatch.")
        if channel.get("token") and channel.get("token") != channel_token:
            raise ValueError("Google Calendar channel token mismatch.")

        calendar_id = str(channel.get("calendar_id") or SOURCE_DOC_ID)
        if resource_state != "sync":
            self._job_ref(calendar_id).set({
                "user_id": self._user_id,
                "calendar_id": calendar_id,
                "status": "pending",
                "requested_at": _to_iso(_utc_now()),
                "resource_state": resource_state,
                "last_message_number": message_number,
                "channel_id": channel_id,
            }, merge=True)

        self._source_ref(calendar_id).set({
            "pending_sync": resource_state != "sync",
            "last_webhook_at": _to_iso(_utc_now()),
            "last_resource_state": resource_state,
            "updated_at": _to_iso(_utc_now()),
        }, merge=True)

        return {
            "channel_id": channel_id,
            "calendar_id": calendar_id,
            "resource_state": resource_state,
        }

    def query_events(
        self,
        *,
        range_name: str | None,
        start_time: str | None,
        end_time: str | None,
        limit: int,
        hours_ahead: int | None = None,
        skip_live_sync: bool = False,
    ) -> dict[str, Any]:
        source = self._load_source()
        integration = self._load_integration()
        if not integration.get("enabled"):
            return {"configured": False, "events": []}

        last_synced_at = _parse_iso(source.get("last_synced_at"))
        pending_sync = bool(source.get("pending_sync"))
        if not skip_live_sync and (
            pending_sync or last_synced_at is None or (
                _utc_now() - last_synced_at > timedelta(minutes=settings.CALENDAR_SYNC_STALE_MINUTES)
            )
        ):
            try:
                self._sync_calendar(reason="chat_query_refresh")
                source = self._load_source()
            except Exception as exc:
                logger.warn("Calendar query refresh failed; continuing with cached data", {
                    "user_id": self._user_id,
                    "error": str(exc),
                })

        tz_name = str(source.get("time_zone") or "UTC")
        tz = ZoneInfo(tz_name)
        now_local = _utc_now().astimezone(tz)

        if start_time and end_time:
            range_start = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(UTC)
            range_end = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(UTC)
        else:
            selected_range = (range_name or "").strip().lower()
            if not selected_range and hours_ahead:
                selected_range = "legacy_hours_ahead"

            if selected_range == "tomorrow":
                day_start = datetime(
                    now_local.year,
                    now_local.month,
                    now_local.day,
                    tzinfo=tz,
                ) + timedelta(days=1)
                range_start = day_start.astimezone(UTC)
                range_end = (day_start + timedelta(days=1)).astimezone(UTC)
            elif selected_range == "this_week":
                day_start = datetime(
                    now_local.year,
                    now_local.month,
                    now_local.day,
                    tzinfo=tz,
                )
                range_start = day_start.astimezone(UTC)
                range_end = (day_start + timedelta(days=7)).astimezone(UTC)
            elif selected_range == "legacy_hours_ahead":
                range_start = _utc_now()
                range_end = range_start + timedelta(hours=max(hours_ahead or 24, 1))
            else:
                day_start = datetime(
                    now_local.year,
                    now_local.month,
                    now_local.day,
                    tzinfo=tz,
                )
                range_start = day_start.astimezone(UTC)
                range_end = (day_start + timedelta(days=1)).astimezone(UTC)

        snapshot = (
            self._events_ref()
            .where("start_at_ts", ">=", range_start)
            .where("start_at_ts", "<", range_end)
            .order_by("start_at_ts")
            .limit(max(limit, 1) * 4)
            .stream()
        )

        events: list[dict[str, Any]] = []
        for doc in snapshot:
            data = doc.to_dict() or {}
            end_at = _coerce_datetime(data.get("end_at_ts"))
            if end_at is not None and end_at <= range_start:
                continue
            if str(data.get("status", "")).lower() == "cancelled":
                continue

            events.append({
                "id": data.get("provider_event_id") or doc.id,
                "title": data.get("summary"),
                "start_time": data.get("start_at"),
                "end_time": data.get("end_at"),
                "location": data.get("location"),
                "status": data.get("status"),
                "attendees": [
                    attendee.get("email")
                    for attendee in data.get("attendees", []) or []
                    if isinstance(attendee, dict) and attendee.get("email")
                ],
                "meeting_link": data.get("hangout_link"),
                "calendar_name": data.get("calendar_name"),
            })
            if len(events) >= limit:
                break

        return {
            "configured": True,
            "events": events,
            "time_zone": tz_name,
        }

    @classmethod
    def for_channel_id(cls, channel_id: str) -> GoogleCalendarConnector | None:
        if not channel_id:
            return None
        doc = admin_firestore().collection(CHANNELS_COLLECTION).document(channel_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        user_id = str(data.get("user_id", "")).strip()
        return cls(user_id) if user_id else None

    @classmethod
    def process_pending_sync_jobs(cls, limit: int = 20) -> int:
        db = admin_firestore()
        jobs = (
            db.collection(SYNC_JOBS_COLLECTION)
            .where("status", "==", "pending")
            .limit(limit)
            .stream()
        )

        processed = 0
        for job_doc in jobs:
            job = job_doc.to_dict() or {}
            user_id = str(job.get("user_id", "")).strip()
            if not user_id:
                job_doc.reference.delete()
                continue

            connector = cls(user_id)
            try:
                connector._sync_calendar(
                    reason=f"webhook_{job.get('resource_state', 'update')}",
                )
                job_doc.reference.set({
                    "status": "completed",
                    "last_processed_at": _to_iso(_utc_now()),
                }, merge=True)
                processed += 1
            except Exception as exc:
                job_doc.reference.set({
                    "status": "error",
                    "last_error": str(exc),
                    "last_processed_at": _to_iso(_utc_now()),
                }, merge=True)
                logger.error("Google Calendar sync job failed", {
                    "user_id": user_id,
                    "calendar_id": job.get("calendar_id"),
                    "error": str(exc),
                })
        return processed

    @classmethod
    def renew_expiring_channels(cls, limit: int = 20) -> int:
        db = admin_firestore()
        threshold = _utc_now() + timedelta(seconds=settings.GOOGLE_CALENDAR_CHANNEL_RENEWAL_LEAD_SECONDS)
        channels = (
            db.collection(CHANNELS_COLLECTION)
            .where("expires_at", "<=", _to_iso(threshold))
            .limit(limit)
            .stream()
        )

        renewed = 0
        for channel_doc in channels:
            channel = channel_doc.to_dict() or {}
            user_id = str(channel.get("user_id", "")).strip()
            watch_url = str(channel.get("watch_url", "")).strip()
            if not user_id or not watch_url:
                continue

            try:
                connector = cls(user_id)
                connector._ensure_watch_channel(watch_url=watch_url)
                renewed += 1
            except Exception as exc:
                logger.error("Google Calendar channel renewal failed", {
                    "user_id": user_id,
                    "calendar_id": channel.get("calendar_id"),
                    "channel_id": channel_doc.id,
                    "error": str(exc),
                })
        return renewed

    @classmethod
    def sync_all_connected_users(cls, max_workers: int = 5) -> dict[str, Any]:
        """Periodic fallback sync for all users with connected calendars.

        Called every 30 minutes from the scheduler tick as a reliability backstop
        when Google push notifications are missed, watch channels expire undetected,
        or deliveries are dropped (which Google explicitly documents as possible).

        Uses incremental sync via sync_token — lightweight delta fetch, not a full
        re-download. On first call or after a 410 token invalidation, falls back
        to a full sync automatically (handled inside _sync_calendar).

        Errors are fully isolated per user. One failure never blocks others.
        OAuth revocations mark the integration as disabled so the user sees a
        reconnect prompt in-app rather than silently getting stale data forever.

        Returns a summary dict for logging in the scheduler tick.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        db = admin_firestore()

        # google_calendar_channels holds one doc per connected user (including
        # users whose channel has expired but was never replaced — they are the
        # ones who most need a fallback sync). Dedup by user_id.
        channel_docs = list(db.collection(CHANNELS_COLLECTION).stream())
        user_ids: list[str] = list({
            uid
            for doc in channel_docs
            if (uid := str((doc.to_dict() or {}).get("user_id", "")).strip())
        })

        if not user_ids:
            return {"users_attempted": 0, "users_synced": 0, "users_skipped": 0, "users_failed": 0}

        synced = 0
        skipped = 0
        failed = 0

        def _sync_one(user_id: str) -> str:
            try:
                connector = cls(user_id)
                integration = connector._load_integration()
                if not integration.get("enabled"):
                    return "skipped"
                connector._sync_calendar(reason="periodic_fallback")
                return "synced"
            except Exception as exc:
                error_str = str(exc)
                # OAuth revoked or token permanently expired — disable the integration
                # so the user is prompted to reconnect rather than seeing stale data.
                if any(kw in error_str.lower() for kw in ("invalid_grant", "token has been expired or revoked", "token_revoked")):
                    try:
                        cls(user_id)._integration_ref().set(
                            {"enabled": False, "last_error": f"OAuth revoked — please reconnect: {error_str[:200]}"},
                            merge=True,
                        )
                    except Exception:
                        pass
                logger.error("GoogleCalendarConnector.sync_all_connected_users: per-user sync failed", {
                    "user_id": user_id,
                    "error": error_str,
                })
                return "failed"

        effective_workers = min(max_workers, len(user_ids))
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = {pool.submit(_sync_one, uid): uid for uid in user_ids}
            for future in as_completed(futures):
                outcome = future.result()
                if outcome == "synced":
                    synced += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1

        summary = {
            "users_attempted": len(user_ids),
            "users_synced": synced,
            "users_skipped": skipped,
            "users_failed": failed,
        }
        logger.info("GoogleCalendarConnector.sync_all_connected_users: complete", summary)
        return summary
