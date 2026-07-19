"""Firestore field-name + collection contract for the reactive layer.

One source of truth (CLAUDE.md data-layer rule): the writer and every reader of
an outbox / processed / event doc reference these constants, never a string
literal. A writer/reader round-trip test breaks CI on a rename so a field-name
drift can never silently return zero rows.

Shared-state layout (per the design doc §5), all under ``users/{uid}/``:
  outbox/{event_id}            the durable bus; ``consumed`` flag drives the relay
  events/{event_id}            consumed/audit log (idempotency + observability)
  processed/{idempotency_key}  idempotent-consumer markers (dedup window TTL)
  intents/{intent_id}          revocable scheduled actions (pending/fired/cancelled)
  resolved_topics/{subject}    resolution tombstones (the late-event resurrection guard)
  cost/{date}                  per-user/day LLM-call counter (the soft cost ceiling)
"""

from __future__ import annotations

from datetime import timedelta

# ── Collections ──────────────────────────────────────────────────────────────
USERS_COLLECTION = "users"
OUTBOX_SUBCOLLECTION = "outbox"
EVENTS_SUBCOLLECTION = "events"
PROCESSED_SUBCOLLECTION = "processed"
INTENTS_SUBCOLLECTION = "intents"
RESOLVED_TOPICS_SUBCOLLECTION = "resolved_topics"
COST_SUBCOLLECTION = "cost"

# ── Event / outbox doc fields ────────────────────────────────────────────────
FIELD_EVENT_ID = "event_id"
FIELD_UID = "uid"
FIELD_TYPE = "type"
FIELD_PAYLOAD = "payload"
FIELD_SOURCE = "source"
FIELD_TS = "ts"
FIELD_SCHEMA_VERSION = "schema_version"
FIELD_DEDUP_ID = "dedup_id"

# Outbox-only fields (the orchestrator's bookkeeping; not part of the Event itself).
# ``consumed`` is the single driver: the relay enqueues an orchestrate for any user
# with ``consumed==false`` events; the orchestrator marks them consumed once it has
# drained + dispatched them. One flag, no lost-after-publish stranding.
FIELD_CONSUMED = "consumed"
FIELD_CONSUMED_AT = "consumed_at"
FIELD_EXPIRES_AT = "expires_at"

# ── Processed-marker fields ──────────────────────────────────────────────────
FIELD_PROCESSED_AT = "processed_at"
# (processed docs reuse FIELD_EXPIRES_AT for their dedup-window TTL)

# ── Pending-intent fields ────────────────────────────────────────────────────
# An intent is a revocable scheduled action: Buddy commits to do X at fire_at, and a
# later event can INVALIDATE it before it fires ("mom is fine" cancels the surgery
# follow-up). ``subject`` is the closed-set resolution key a resolution event matches.
FIELD_INTENT_ID = "intent_id"
FIELD_KIND = "kind"
FIELD_SUBJECT = "subject"
FIELD_QUESTION = "question"
FIELD_FIRE_AT = "fire_at"
FIELD_STATUS = "status"
FIELD_CREATED_AT = "created_at"
FIELD_SESSION_ID = "session_id"
FIELD_FIRED_AT = "fired_at"
FIELD_CANCELLED_AT = "cancelled_at"
FIELD_RESOLUTION_REASON = "resolution_reason"

# Intent lifecycle states.
INTENT_PENDING = "pending"
INTENT_FIRED = "fired"
INTENT_CANCELLED = "cancelled"
INTENT_EXPIRED = "expired"

# ── Cost-cap fields ──────────────────────────────────────────────────────────
FIELD_LLM_CALLS = "llm_calls"

# ── TTLs (native Firestore TTL on expires_at reaps these) ────────────────────
# Outbox rows are disposable once consumed; keep a short tail so a late sweep crash
# still has the row to re-dispatch, then native TTL reaps it.
OUTBOX_TTL = timedelta(days=2)
# A processed marker only needs to outlive the duplicate-delivery window. Cloud
# Tasks retries and out-of-order delivery resolve well inside a day; keep two so
# a slow duplicate still dedups.
PROCESSED_TTL = timedelta(days=2)
# A fired/cancelled intent lingers briefly for observability, then native TTL reaps
# it. A pending intent's expires_at is its own fire_at + a grace, so a never-fired
# intent cannot haunt the collection forever.
INTENT_TTL = timedelta(days=14)
# The resurrection guard must outlive any late resolution that could re-create a
# just-resolved follow-up. A week covers the realistic "I already told you" window.
RESOLVED_TOPIC_TTL = timedelta(days=7)
