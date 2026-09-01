"""Append-only Meeting V2 audit events plus the create-only txn primitive.

Shared by ``store.py``, ``tasks.py``, and ``deletion.py`` (previously private
``store._audit_event`` / ``store._txn_create`` that both siblings reached
into).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from . import fields as F
from . import refs


def txn_create(txn: Any, ref: Any, value: dict[str, Any]) -> None:
    """Use Firestore's create-only primitive; old in-repo fakes fall back to set."""
    create = getattr(txn, "create", None)
    if callable(create):
        create(ref, value)
    else:
        txn.set(ref, value)


def actor_hash(actor_identity: str) -> str:
    return hashlib.sha256(actor_identity.encode("utf-8")).hexdigest()


def audit_event(
    txn: Any,
    *,
    uid: str,
    meeting_id: str,
    sequence: int,
    event_type: str,
    occurred_at: str,
    actor_type: str = "server",
    actor_identity: str = "juno-backend",
    runtime_instance_id: str = "",
    capture_run_id: str = "",
    capture_fence: int = 0,
    job_id: str = "",
    attempt: int = 0,
    lease_token: str = "",
    prior_state: str = "",
    next_state: str = "",
    artifacts: list[dict[str, Any]] | None = None,
    reason_code: str = "",
    correlation_id: str = "",
    causation_id: str = "",
    policy_version: str = "",
) -> str:
    event_id = uuid.uuid4().hex
    envelope = {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "recorded_at": datetime.now(UTC).isoformat(),
        "actor_type": actor_type,
        "actor_identity_hash": actor_hash(actor_identity),
        F.RUNTIME_INSTANCE_ID: runtime_instance_id,
        "meeting_id": meeting_id,
        F.CAPTURE_RUN_ID: capture_run_id,
        F.CAPTURE_FENCE: capture_fence,
        "job_id": job_id,
        "attempt": attempt,
        "lease_token_hash": actor_hash(lease_token) if lease_token else "",
        "prior_state": prior_state,
        "next_state": next_state,
        "artifacts": artifacts or [],
        "reason_code": reason_code,
        "software_version": F.SOFTWARE_COMPONENT,
        "schema_version": F.AUDIT_SCHEMA_VERSION,
        "policy_version": policy_version,
        "correlation_id": correlation_id or event_id,
        "causation_id": causation_id,
    }
    txn_create(txn, refs.audit_ref(uid, meeting_id).document(event_id), envelope)
    return event_id
