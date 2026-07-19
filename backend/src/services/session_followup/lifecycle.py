"""Single owner of session lifecycle state and session-finalized events."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from google.cloud import firestore as fs

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF
from ..reactive import event_bus
from ..reactive.events import EVENT_SESSION_FINALIZED
from . import fields as F

_LEXICAL_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
_SWEEP_LIMIT = 100


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _clean_lineage(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:20]


class SessionLifecycleService:
    """The only service allowed to transition a session to ``finalized``."""

    def __init__(self, db: Any | None = None) -> None:
        self._db = db

    @property
    def db(self):
        return self._db or admin_firestore()

    def _user_ref(self, uid: str):
        return self.db.collection(GF.PARENT_COLLECTION).document(uid)

    def _session_ref(self, uid: str, session_id: str):
        return self._user_ref(uid).collection(F.SESSIONS).document(session_id)

    async def start_session(
        self,
        uid: str,
        session_id: str | None,
        *,
        surface: str,
        origin: str = F.ORIGIN_ORGANIC,
        origin_candidate_id: str | None = None,
        lineage_chain: list[str] | None = None,
        now: datetime | None = None,
    ) -> str:
        """Create a session root or resume a requested voice session inside grace."""
        if not F.feature_enabled(settings):
            return session_id or ""
        when = now or datetime.now(UTC)
        resolved_id = session_id or f"sess_{uuid4().hex}"
        if surface == F.SURFACE_VOICE and not session_id:
            resumed = await self._find_voice_session_in_grace(uid, when)
            if resumed:
                resolved_id = resumed

        def _write() -> None:
            ref = self._session_ref(uid, resolved_id)
            snap = ref.get()
            current = (snap.to_dict() or {}) if snap.exists else {}
            data = {
                "session_id": resolved_id,
                "surface": surface,
                "origin": current.get("origin") or origin or F.ORIGIN_ORGANIC,
                "origin_candidate_id": (
                    current.get("origin_candidate_id") or origin_candidate_id
                ),
                "lineage_chain": current.get("lineage_chain") or _clean_lineage(
                    lineage_chain
                ),
                "state": F.STATE_ACTIVE,
                "started_at": current.get("started_at") or when,
                "last_activity_at": when,
                "last_user_turn_at": current.get("last_user_turn_at"),
                "input_revision": max(1, int(current.get("input_revision", 1) or 1)),
                "turn_count": int(current.get("turn_count", 0) or 0),
                "user_turn_count": int(current.get("user_turn_count", 0) or 0),
                "finalization": {
                    **F.FINALIZATION_DEFAULTS,
                    "reason": None,
                },
                "disconnect_grace_until": None,
            }
            ref.set(data, merge=True)

        await asyncio.to_thread(_write)
        return resolved_id

    async def _find_voice_session_in_grace(
        self, uid: str, now: datetime
    ) -> str | None:
        def _read() -> str | None:
            query = (
                self._user_ref(uid)
                .collection(F.SESSIONS)
                .where(filter=fs.FieldFilter("state", "==", F.STATE_DISCONNECT_GRACE))
                .order_by("last_activity_at", direction="DESCENDING")
                .limit(5)
            )
            for snap in query.stream():
                data = snap.to_dict() or {}
                grace_until = _aware(data.get("disconnect_grace_until"))
                if data.get("surface") == F.SURFACE_VOICE and grace_until and grace_until > now:
                    return snap.id
            return None

        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            logger.warn("session_followup: voice resume lookup failed open", {
                "user_id": uid,
                "error": str(exc),
            })
            return None

    async def note_user_turn(
        self,
        uid: str,
        session_id: str,
        *,
        surface: str,
        turn_id: str,
        turn_index: int,
        text: str,
        entity_keys: list[str] | None = None,
        input_revision: int = 1,
        origin: str = F.ORIGIN_ORGANIC,
        origin_candidate_id: str | None = None,
        lineage_chain: list[str] | None = None,
        inferred_sensitive: bool = False,
        reminder_created_in_session: bool = False,
        signals: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write one immutable turn and update the mutable session root transactionally."""
        if not F.feature_enabled(settings) or not uid or not session_id or not turn_id:
            return
        when = now or datetime.now(UTC)
        clean_text = text.strip()
        lexical_terms = sorted({
            token.casefold() for token in _LEXICAL_TOKEN.findall(clean_text)
        })[:24]
        topic_identity = sorted({
            GF.normalized_entity_key(str(value))
            for value in entity_keys or []
            if str(value).strip()
        }) or lexical_terms[:6]
        active_topic_id = (
            f"topic_{hashlib.sha1('|'.join(topic_identity).encode()).hexdigest()[:24]}"
            if topic_identity
            else None
        )

        def _write() -> None:
            session_ref = self._session_ref(uid, session_id)
            turn_ref = session_ref.collection(F.TURNS).document(turn_id)
            transaction = self.db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> None:
                session_snap = session_ref.get(transaction=txn)
                session = (session_snap.to_dict() or {}) if session_snap.exists else {}
                turn_snap = turn_ref.get(transaction=txn)
                is_new = not turn_snap.exists
                if is_new:
                    txn.create(turn_ref, {
                        "turn_index": max(0, int(turn_index)),
                        "role": "user",
                        "entity_keys": sorted({
                            str(value).strip()
                            for value in entity_keys or []
                            if str(value).strip()
                        }),
                        "text_hash": hashlib.sha1(clean_text.encode("utf-8")).hexdigest(),
                        "embedding_ref": None,
                        "lexical_terms": lexical_terms,
                        "revision": max(1, int(input_revision)),
                        "created_at": when,
                        "inferred_sensitive": bool(inferred_sensitive),
                        "reminder_created_in_session": bool(reminder_created_in_session),
                        **dict(signals or {}),
                    })
                txn.set(session_ref, {
                    "session_id": session_id,
                    "surface": surface,
                    "origin": session.get("origin") or origin or F.ORIGIN_ORGANIC,
                    "origin_candidate_id": (
                        session.get("origin_candidate_id") or origin_candidate_id
                    ),
                    "lineage_chain": session.get("lineage_chain") or _clean_lineage(lineage_chain),
                    "state": F.STATE_ACTIVE,
                    "started_at": session.get("started_at") or when,
                    "last_activity_at": when,
                    "last_user_turn_at": when,
                    "active_topic_id": active_topic_id,
                    "input_revision": max(
                        int(session.get("input_revision", 1) or 1),
                        max(1, int(input_revision)),
                    ),
                    "turn_count": int(session.get("turn_count", 0) or 0) + int(is_new),
                    "user_turn_count": int(session.get("user_turn_count", 0) or 0) + int(is_new),
                    "finalization": {
                        **F.FINALIZATION_DEFAULTS,
                        "reason": None,
                    },
                    "disconnect_grace_until": None,
                }, merge=True)

            _apply(transaction)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.warn("session_followup: turn write failed open", {
                "user_id": uid,
                "session_id": session_id,
                "error": str(exc),
            })

    async def note_voice_disconnect(
        self, uid: str, session_id: str, *, now: datetime | None = None
    ) -> None:
        if not F.feature_enabled(settings):
            return
        when = now or datetime.now(UTC)

        def _write() -> None:
            ref = self._session_ref(uid, session_id)
            snap = ref.get()
            if snap.exists and (snap.to_dict() or {}).get("state") == F.STATE_FINALIZED:
                return
            ref.set({
                "state": F.STATE_DISCONNECT_GRACE,
                "last_activity_at": when,
                "disconnect_grace_until": when + F.VOICE_DISCONNECT_GRACE,
            }, merge=True)

        await asyncio.to_thread(_write)

    async def finalize_session(
        self,
        uid: str,
        session_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Finalize once and atomically stage the sole session-finalized event."""
        if not F.feature_enabled(settings):
            return False
        when = now or datetime.now(UTC)

        def _finalize() -> bool:
            session_ref = self._session_ref(uid, session_id)
            transaction = self.db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> bool:
                snap = session_ref.get(transaction=txn)
                if not snap.exists:
                    return False
                session = snap.to_dict() or {}
                if session.get("state") == F.STATE_FINALIZED:
                    return False
                revision = max(1, int(session.get("input_revision", 1) or 1))
                payload = {
                    "uid": uid,
                    "session_id": session_id,
                    "surface": str(session.get("surface") or "unknown"),
                    "origin": str(session.get("origin") or F.ORIGIN_ORGANIC),
                    "input_revision": revision,
                }
                event = event_bus.build_event(
                    uid,
                    EVENT_SESSION_FINALIZED,
                    payload,
                    source=F.SOURCE_SESSION_FOLLOWUP,
                    dedup_id=f"session-finalized:{session_id}:{revision}",
                    ts=when,
                )
                txn.set(session_ref, {
                    "state": F.STATE_FINALIZED,
                    "finalized_at": when,
                    "disconnect_grace_until": None,
                    "finalization": {
                        **F.FINALIZATION_DEFAULTS,
                        "reason": reason,
                    },
                }, merge=True)
                event_bus.stage_event(txn, event)
                return True

            return _apply(transaction)

        try:
            finalized = await asyncio.to_thread(_finalize)
        except Exception as exc:
            logger.error("session_followup: finalization transaction failed", {
                "user_id": uid,
                "session_id": session_id,
                "error": str(exc),
            })
            return False
        if finalized:
            from .evaluator import evaluate_finalized_session

            await evaluate_finalized_session(uid, session_id, now=when)
        return finalized

    async def sweep_idle_sessions(self, *, now: datetime | None = None) -> int:
        """Finalize globally due chat, voice-idle, and expired-grace sessions."""
        if not F.feature_enabled(settings):
            return 0
        when = now or datetime.now(UTC)
        discovery_cutoff = when - F.VOICE_DISCONNECT_GRACE

        def _read() -> list[tuple[str, str, dict[str, Any]]]:
            snaps = list(
                self.db.collection_group(F.SESSIONS)
                .where(filter=fs.FieldFilter("state", "in", [
                    F.STATE_ACTIVE,
                    F.STATE_DISCONNECT_GRACE,
                ]))
                .where(filter=fs.FieldFilter("last_activity_at", "<=", discovery_cutoff))
                .order_by("last_activity_at")
                .limit(_SWEEP_LIMIT)
                .stream()
            )
            result = []
            for snap in snaps:
                user_ref = snap.reference.parent.parent
                if user_ref is not None:
                    result.append((user_ref.id, snap.id, snap.to_dict() or {}))
            return result

        try:
            sessions = await asyncio.to_thread(_read)
        except Exception as exc:
            logger.error("session_followup: idle sweep failed (missing index?)", {
                "error": str(exc),
            })
            return 0
        finalized = 0
        for uid, session_id, session in sessions:
            state = str(session.get("state") or "")
            surface = str(session.get("surface") or "")
            last_user = _aware(session.get("last_user_turn_at")) or _aware(
                session.get("last_activity_at")
            )
            grace_until = _aware(session.get("disconnect_grace_until"))
            due = False
            reason = "idle_timeout"
            if state == F.STATE_DISCONNECT_GRACE:
                due = grace_until is None or grace_until <= when
                reason = "disconnect_grace_elapsed"
            elif surface == F.SURFACE_CHAT:
                due = last_user is not None and when - last_user >= F.CHAT_IDLE_TIMEOUT
            elif surface == F.SURFACE_VOICE:
                due = last_user is not None and when - last_user >= F.VOICE_IDLE_TIMEOUT
            if due and await self.finalize_session(uid, session_id, reason=reason, now=when):
                finalized += 1
        return finalized


session_lifecycle_service = SessionLifecycleService()
