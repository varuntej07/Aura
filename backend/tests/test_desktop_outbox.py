from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from google.api_core.exceptions import AlreadyExists

from src.handlers import desktop_notifications as handler
from src.services.notifications import desktop_outbox as outbox
from src.services.notifications.proposal import (
    SOURCE_MEETING,
    DeliveryChannel,
    NotificationProposal,
    ProposalKind,
)

NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


def _proposal(*, action: str = "view_meeting") -> NotificationProposal:
    return NotificationProposal(
        user_id="user-1",
        source=SOURCE_MEETING,
        kind=ProposalKind.COMMITTED,
        dedup_key="meeting:m1:ready:2",
        title="Your meeting note is ready",
        body="Open Aura to view it.",
        notification_type="meeting_ready",
        channels=frozenset({DeliveryChannel.DESKTOP}),
        data={
            "severity": "success",
            "toast_policy": "when_hidden",
            "action": action,
            "resource_id": "m1",
            "sensitive": "true",
        },
    )


def test_build_document_matches_versioned_contract_and_caps_expiry():
    proposal = _proposal()
    proposal.data["expires_at"] = (NOW + timedelta(days=90)).isoformat()

    doc = outbox.build_document(proposal, "notification-1", now=NOW)

    assert doc[outbox.FIELD_SCHEMA_VERSION] == 1
    assert doc[outbox.FIELD_TYPE] == "meeting_ready"
    assert doc[outbox.FIELD_ACTION] == "view_meeting"
    assert doc[outbox.FIELD_SENSITIVE] is True
    assert doc[outbox.FIELD_EXPIRES_AT] == NOW + timedelta(days=30)


def test_build_document_rejects_malformed_action():
    with pytest.raises(ValueError, match="action"):
        outbox.build_document(_proposal(action="run_shell"), "n1", now=NOW)


def test_unknown_mobile_type_is_safely_mapped_to_generic_desktop_contract():
    proposal = _proposal()
    proposal.notification_type = "reminder"
    proposal.source = "followup"
    proposal.data = {}

    doc = outbox.build_document(proposal, "n1", now=NOW)

    assert doc[outbox.FIELD_TYPE] == "generic"
    assert doc[outbox.FIELD_ACTION] == "open_notifications"
    assert doc[outbox.FIELD_TOAST_POLICY] == "when_hidden"
    assert doc[outbox.FIELD_SENSITIVE] is True


async def test_enqueue_is_idempotent_when_document_exists(monkeypatch):
    class Ref:
        def create(self, doc):
            raise AlreadyExists("exists")

    class Collection:
        def document(self, notification_id):
            return Ref()

    monkeypatch.setattr(outbox, "_collection", lambda user_id: Collection())

    result = await outbox.enqueue(_proposal(), "n1", now=NOW)

    assert result == outbox.OutboxWriteResult(accepted=True, created=False)


class _Snap:
    def __init__(self, notification_id: str, data: dict, *, exists: bool = True):
        self.id = notification_id
        self._data = data
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(self, rows: list[_Snap]):
        self.rows = rows
        self.after = ""
        self.count = len(rows)

    def order_by(self, field):
        return self

    def start_after(self, snap):
        self.after = snap.id
        return self

    def limit(self, count):
        self.count = count
        return self

    def stream(self):
        rows = self.rows
        if self.after:
            index = next(i for i, row in enumerate(rows) if row.id == self.after)
            rows = rows[index + 1 :]
        return rows[: self.count]


class _Collection(_Query):
    def document(self, notification_id):
        for row in self.rows:
            if row.id == notification_id:
                return _Ref(row)
        return _Ref(_Snap(notification_id, {}, exists=False))


class _Ref:
    def __init__(self, snap: _Snap):
        self.snap = snap

    def get(self):
        return self.snap

    def update(self, updates):
        self.snap._data.update(updates)


async def test_cursor_pagination_skips_expired_rows(monkeypatch):
    rows = [
        _Snap("n1", {
            outbox.FIELD_CREATED_AT: NOW,
            outbox.FIELD_EXPIRES_AT: NOW - timedelta(days=1),
        }),
        _Snap("n2", {
            outbox.FIELD_CREATED_AT: NOW,
            outbox.FIELD_EXPIRES_AT: NOW + timedelta(days=1),
        }),
        _Snap("n3", {
            outbox.FIELD_CREATED_AT: NOW,
            outbox.FIELD_EXPIRES_AT: NOW + timedelta(days=1),
        }),
    ]
    collection = _Collection(rows)
    monkeypatch.setattr(outbox, "_collection", lambda user_id: collection)

    first, cursor = await outbox.list_notifications("user-1", limit=2, now=NOW)
    second, next_cursor = await outbox.list_notifications(
        "user-1", cursor=cursor or "", limit=2, now=NOW
    )

    assert [item[outbox.FIELD_NOTIFICATION_ID] for item in first] == ["n2"]
    assert [item[outbox.FIELD_NOTIFICATION_ID] for item in second] == ["n3"]
    assert next_cursor is not None


async def test_acknowledgement_is_monotonic_and_action_is_allowlisted(monkeypatch):
    snap = _Snap("n1", {
        outbox.FIELD_ACTION: "view_meeting",
        outbox.FIELD_DELIVERY_STATUS: outbox.STATUS_QUEUED,
        outbox.FIELD_EXPIRES_AT: NOW + timedelta(days=30),
        outbox.FIELD_RECEIVED_AT: None,
        outbox.FIELD_SEEN_AT: None,
        outbox.FIELD_ACTED_AT: None,
    })
    monkeypatch.setattr(outbox, "_collection", lambda user_id: _Collection([snap]))

    assert await outbox.acknowledge(
        "user-1", "n1", status=outbox.STATUS_ACTED, action="view_meeting", now=NOW
    )
    assert await outbox.acknowledge(
        "user-1", "n1", status=outbox.STATUS_RECEIVED, now=NOW
    )
    assert snap._data[outbox.FIELD_DELIVERY_STATUS] == outbox.STATUS_ACTED
    with pytest.raises(ValueError, match="action"):
        await outbox.acknowledge(
            "user-1", "n1", status=outbox.STATUS_ACTED, action="run_shell", now=NOW
        )


class _Request:
    query_params = {}

    async def json(self):
        return {"status": "received"}


async def test_handler_uses_authenticated_owner_and_rejects_unauthenticated(monkeypatch):
    seen: list[str] = []

    async def list_notifications(user_id, **kwargs):
        seen.append(user_id)
        return [], None

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda request: "owner-1")
    monkeypatch.setattr(handler.desktop_outbox, "list_notifications", list_notifications)
    response = await handler.handle_list(_Request())
    assert response.status_code == 200
    assert seen == ["owner-1"]

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda request: None)
    response = await handler.handle_list(_Request())
    assert response.status_code == 401


class _PreferencesRequest:
    async def json(self):
        return {
            "enabled": True,
            "committed_enabled": True,
            "proactive_enabled": False,
            "account_enabled": True,
        }


async def test_preference_update_is_owner_scoped_and_strict(monkeypatch):
    seen = []

    async def update(user_id, preferences):
        seen.append((user_id, preferences))
        return preferences

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda request: "owner-1")
    monkeypatch.setattr(handler.desktop_preferences, "update", update)

    response = await handler.handle_update_preferences(_PreferencesRequest())

    assert response.status_code == 200
    assert seen[0][0] == "owner-1"
    assert seen[0][1].enabled is True
    assert seen[0][1].proactive_enabled is False


async def test_acknowledgement_updates_the_shared_logical_ledger(monkeypatch):
    acknowledgements = []

    async def acknowledge(*args, **kwargs):
        return True

    async def record(user_id, notification_id, *, status):
        acknowledgements.append((user_id, notification_id, status))

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda request: "owner-1")
    monkeypatch.setattr(handler.desktop_outbox, "acknowledge", acknowledge)
    monkeypatch.setattr(handler.notification_ledger, "record_desktop_ack", record)

    response = await handler.handle_acknowledge(_Request(), "notification-1")

    assert response.status_code == 200
    assert acknowledgements == [("owner-1", "notification-1", "received")]
