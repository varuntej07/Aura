"""Research run Firestore contract and stable public enums.

Constants only, zero logic, following ``services/meetings/fields.py`` exactly. Nothing
here is registered: no route, tool, Cloud Task target, or scheduler hook reads it yet.
It exists so the durable engine phase and the contracts in ``models.py`` agree on one
spelling of every field name and every state.

Firestore layout. A run cannot be one document: a 60-source run with 150 claims would
pass Firestore's 1 MB per-document limit, so evidence lives in subcollections and the
hot budget counter is isolated from the state doc it would otherwise contend with.

    users/{uid}/research_runs/{run_id}
      /ledger/budget                  hot counters, isolated from the state doc
      /coord/{wave_id}                fan-out completion counter + join claim
      /stages/{stage_id}              one doc per unit of work, the idempotency anchor
      /plans/{plan_version}           generated scope, assumptions, policy, sub-questions
      /sources/{source_id}            sha256(canonical_url)[:24] + candidate claims
      /claims/{claim_id}
      /audit_events/{sequence}
    users/{uid}/research_jobs/{job_id}
    users/{uid}/research_job_outbox/{job_id}
    users/{uid}/research_deletions/{run_id}
    research_domain_classes/{sha256(domain)[:24]}      GLOBAL, cross-user, 30d TTL

``research_domain_classes`` is deliberately a ROOT collection. A domain's epistemic
class is not user data, and amortising the classification across every user is the
entire point of caching it. It stores registrable domain, class, confidence, vote
count and expiry, and never the query, entity, page title, snippet, uid, or run id.

Retention: every run-owned document carries the same EXPIRES_AT, because native
Firestore TTL does not recursively delete subcollections. Expiring only the parent run
would orphan its sources, claims, stages, budget and audit trail. Explicit user
deletion goes through RESEARCH_DELETIONS rather than relying on TTL timing.
"""

from __future__ import annotations

# --- Firestore locations -------------------------------------------------------
PARENT_COLLECTION = "users"
SUBCOLLECTION = "research_runs"
JOBS_SUBCOLLECTION = "research_jobs"
JOB_OUTBOX_SUBCOLLECTION = "research_job_outbox"
DELETIONS_SUBCOLLECTION = "research_deletions"

# Run-owned subcollections.
LEDGER_SUBCOLLECTION = "ledger"
LEDGER_BUDGET_DOC = "budget"
COORD_SUBCOLLECTION = "coord"
STAGES_SUBCOLLECTION = "stages"
PLANS_SUBCOLLECTION = "plans"
SOURCES_SUBCOLLECTION = "sources"
CLAIMS_SUBCOLLECTION = "claims"
AUDIT_SUBCOLLECTION = "audit_events"

# The one root collection. Cross-user by design; carries no user data.
DOMAIN_CLASS_COLLECTION = "research_domain_classes"

# Weighted daily credit counter, on the user-day accounting boundary the rest of the
# product already uses. Deliberately NOT the meetings monthly doc and deliberately NOT
# reactive/cost_cap's DAILY_LLM_CALL_CAP document: research cannot share a 100-call
# budget with the reactive loop without starving it.
USAGE_SUBCOLLECTION = "usage"
USAGE_DOC_PREFIX = "research_"  # research_{YYYY-MM-DD}

# Project-wide daily spend reservation. A root collection because the ceiling is a
# property of the project's wallet, not of any one user.
PROJECT_BUDGET_COLLECTION = "research_project_budget"
# One receipt per stage ATTEMPT that reserved project spend. A subcollection of the day
# document, so a receipt physically carries the day it was written against.
#
# The aggregate counter alone could not be settled correctly. Reconciliation recomputed
# the day from the clock, so a stage that reserved at 23:59 and finished at 00:01 debited
# a day it had never credited and could drive that day's reserved figure negative. It also
# leaked on every exit that was not a clean success, because a bare pair of counters has
# no record of WHICH stage still owes a settlement. A receipt has both: its own day, and
# its own amount.
PROJECT_RECEIPTS_SUBCOLLECTION = "receipts"
PROJECT_RECEIPTS_DELETION_TARGET = "project_receipts"
PROJECT_RESERVED_MICROUSD = "reserved_microusd"
PROJECT_ACTUAL_MICROUSD = "actual_microusd"
RECEIPT_DAY = "day"
RECEIPT_ESTIMATE_MICROUSD = "estimate_microusd"
RECEIPT_ACTUAL_MICROUSD = "actual_microusd"
RECEIPT_STATE = "state"
RECEIPT_RESERVED = "reserved"
RECEIPT_SETTLED = "settled"
RECEIPT_RELEASED = "released"
RECEIPT_COST_KNOWN = "cost_known"
RECEIPT_STAGE_ID = "stage_id"
RECEIPT_ATTEMPT = "attempt"
RECEIPT_RUN_ID = "run_id"
RECEIPT_USER_ID = "user_id"
RECEIPT_DEADLINE_AT = "deadline_at"
# A settled receipt lingers as proof the reservation was closed, then native TTL reaps it.
PROJECT_RECEIPT_TTL_DAYS = 7

# Every run-owned collection that a deletion receipt must drain and that needs a TTL
# field override configured. Deleting the parent alone leaves all of these behind.
RUN_OWNED_SUBCOLLECTIONS = (
    LEDGER_SUBCOLLECTION,
    COORD_SUBCOLLECTION,
    STAGES_SUBCOLLECTION,
    PLANS_SUBCOLLECTION,
    SOURCES_SUBCOLLECTION,
    CLAIMS_SUBCOLLECTION,
    AUDIT_SUBCOLLECTION,
)

# Collections that belong to ONE run but live under the USER document rather than under
# the run, because the outbox sweep has to reach them with a single collection-group
# query. They are still that run's data: a job row carries the stage payload (including
# discovered URLs) and stays redispatchable, so a deletion that skipped them left a
# deleted run's URLs on disk and a stage that could still be handed to a worker.
RUN_SCOPED_USER_COLLECTIONS = (
    JOBS_SUBCOLLECTION,
    JOB_OUTBOX_SUBCOLLECTION,
)

# The full walk order for an explicit deletion. APPENDED to, never reordered: a receipt
# persists the index it reached, so inserting a collection would make every in-flight
# receipt resume against the wrong one.
DELETION_COLLECTIONS = (
    RUN_OWNED_SUBCOLLECTIONS
    + RUN_SCOPED_USER_COLLECTIONS
    + (PROJECT_RECEIPTS_DELETION_TARGET,)
)

# --- run doc fields ------------------------------------------------------------
RUN_ID = "run_id"
CLIENT_RUN_ID = "client_run_id"
ORIGIN_SURFACE = "origin_surface"
PRESET = "preset"
REQUEST_TEXT = "request"
REQUEST_REVISION = "request_revision"
CURRENT_PLAN_VERSION = "current_plan_version"
# Pinned by the admission transaction. Every later stage refuses a different version,
# so a delayed task can never execute against a newer interpretation than the one the
# user actually confirmed.
ADMITTED_PLAN_VERSION = "admitted_plan_version"
AUTO_ADMIT_REQUESTED = "auto_admit_requested"
CLARIFICATION_ANSWERS = "clarification_answers"
PENDING_QUESTION = "pending_question"
PENDING_QUESTION_EXPIRES_AT = "pending_question_expires_at"
CLARIFICATION_ROUNDS = "clarification_rounds"

STATE = "state"
PROCESSING_STAGE = "processing_stage"
STATE_REVISION = "state_revision"
AUDIT_SEQUENCE = "audit_sequence"
FAILURE_CODE = "failure_code"
CANCEL_REQUESTED_AT = "cancel_requested_at"
DEADLINE_AT = "deadline_at"
CREATED_AT = "created_at"
UPDATED_AT = "updated_at"
EXPIRES_AT = "expires_at"

CREDIT_RECEIPT = "credit_receipt"
CREDIT_WEIGHT = "credit_weight"
BRIEF = "brief"
GAPS = "gaps"
EVIDENCE_AS_OF = "evidence_as_of"
RETRIEVED_AT_RANGE = "retrieved_at_range"
SOURCE_COUNT = "source_count"
CLAIM_COUNT = "claim_count"
EFFECTIVE_POLICY = "effective_policy"
HIDDEN_AT = "hidden_at"

# --- stage doc / lease fields --------------------------------------------------
STAGE_ID = "stage_id"
STAGE_KIND = "stage_kind"
STAGE_STATE = "state"
STAGE_ATTEMPT = "stage_attempt"
STAGE_DEADLINE_AT = "stage_deadline_at"
STAGE_PROJECT_RECEIPT_DAY = "project_receipt_day"
STAGE_PROJECT_RECEIPT_ID = "project_receipt_id"
LEASE_TOKEN_HASH = "lease_token_hash"
DISPATCH_ATTEMPTS = "dispatch_attempts"
DISPATCH_DUE_AT = "dispatch_due_at"
TASK_NAME = "task_name"
WAVE = "wave"

STAGE_PENDING = "pending"
STAGE_DISPATCHED = "dispatched"
STAGE_LEASED = "leased"
STAGE_DONE = "done"
STAGE_ABANDONED = "abandoned"
STAGE_FAILED = "failed"
# Stages a sweeper may steal the lease from once STAGE_DEADLINE_AT has passed.
STAGE_ACTIVE_STATES = (STAGE_DISPATCHED, STAGE_LEASED)

# --- fan-out coordination ------------------------------------------------------
COORD_EXPECTED = "expected"
COORD_COMPLETED = "completed"
COORD_JOIN_CLAIMED = "join_claimed"
COORD_JOIN_JOB_ID = "join_job_id"
COORD_DEADLINE_AT = "deadline_at"

# --- job + outbox doc fields ---------------------------------------------------
# stage_id == job_id == outbox_id. One identity across three documents, so a replayed
# advance collides on create() in all three at once rather than needing its own guard.
JOB_ID = "job_id"
JOB_KIND = "kind"
JOB_STATE = "state"
JOB_RUN_ID = "run_id"
JOB_USER_ID = "user_id"
OUTBOX_ID = "outbox_id"
OUTBOX_STATE = "state"
LAST_DISPATCHED_AT = "last_dispatched_at"
NEXT_ATTEMPT_AT = "next_attempt_at"
LAST_ERROR_CODE = "last_error_code"
CORRELATION_ID = "correlation_id"
CAUSATION_ID = "causation_id"

OUTBOX_PENDING = "pending"
OUTBOX_DISPATCHED = "dispatched"
OUTBOX_RETRY = "retry"
# Terminal. The stage it delivered has finished and this row must never be selected
# again. Recorded rather than deleted so the delivery history stays auditable until the
# row's own TTL removes it.
OUTBOX_DONE = "done"
# States dispatch_pending picks up. DISPATCHED is included on purpose so a dispatch
# that was accepted but never delivered can still be rescued by the stale predicate.
OUTBOX_DISPATCHABLE_STATES = (OUTBOX_PENDING, OUTBOX_RETRY, OUTBOX_DISPATCHED)
# How long a "dispatched" row may sit before the sweeper treats it as never delivered.
STALE_DISPATCH_MINUTES = 10

# --- ledger doc fields ---------------------------------------------------------
# The ledger lives at .../ledger/budget, isolated from the run doc so the hot counter
# never contends with the state document a status read touches.
LEDGER_USED = "used"
LEDGER_RESERVED = "reserved"
LEDGER_RESERVATIONS = "reservations"  # {stage_id: {unit: granted}}
LEDGER_COST_MICROUSD = "cost_microusd"
# Micro-USD reserved by stages that have not settled yet. Held alongside LEDGER_COST_MICROUSD
# (which is actual spend) so the per-run dollar ceiling can be checked as
# actual + reserved + requested <= max, in the same transaction as the unit reservation.
# Without it the run had unit ceilings and no dollar ceiling at all: a run could stay
# inside every unit budget and still cost far more than intended, because unit prices
# differ by two orders of magnitude between a Brave query and an expert-tier synthesis.
LEDGER_RESERVED_MICROUSD = "reserved_microusd"
# The stage-keyed micro-USD reservations behind LEDGER_RESERVED_MICROUSD, so releasing one
# stage's dollars never has to guess an amount. Same shape and same reason as
# LEDGER_RESERVATIONS.
LEDGER_COST_RESERVATIONS = "cost_reservations"
# {unit: units consumed BEYOND what was granted}, cumulative for the run. Recorded
# separately from `used` so that "a stage outspent its reservation" is answerable from
# the ledger document alone. A non-empty value here means a reservation in
# STAGE_UNIT_REQUESTS is smaller than what its stage can actually spend.
LEDGER_OVERRUN = "overrun"
LEDGER_UPDATED_AT = "updated_at"

# Every meterable unit. Named here so the ledger, the budget preset and the audit
# trail cannot drift apart on spelling.
UNIT_SEARCHES = "searches"
UNIT_EXTRACTS = "extracts"
UNIT_PAGE_CREDITS = "page_credits"
UNIT_BYTES = "bytes"
UNIT_MODEL_CALLS = "model_calls"
UNIT_MODEL_INPUT_TOKENS = "model_input_tokens"
UNIT_MODEL_OUTPUT_TOKENS = "model_output_tokens"
ALL_UNITS = (
    UNIT_SEARCHES,
    UNIT_EXTRACTS,
    UNIT_PAGE_CREDITS,
    UNIT_BYTES,
    UNIT_MODEL_CALLS,
    UNIT_MODEL_INPUT_TOKENS,
    UNIT_MODEL_OUTPUT_TOKENS,
)
# unit name -> the RunBudget attribute holding its ceiling.
UNIT_BUDGET_ATTR = {
    UNIT_SEARCHES: "searches_max",
    UNIT_EXTRACTS: "extracts_max",
    UNIT_PAGE_CREDITS: "page_credits_max",
    UNIT_BYTES: "bytes_max",
    UNIT_MODEL_CALLS: "model_calls_max",
    UNIT_MODEL_INPUT_TOKENS: "model_input_tokens_max",
    UNIT_MODEL_OUTPUT_TOKENS: "model_output_tokens_max",
}

# --- deletion receipt fields ---------------------------------------------------
# A receipt is resumable: it records WHICH collection it was draining and the last doc
# id it deleted, so a retry after a crash continues rather than restarting the subtree.
DELETION_RECEIPT_STATE = "state"
DELETION_PENDING = "pending"
DELETION_RUNNING = "running"
DELETION_DONE = "complete"
DELETION_COLLECTION_INDEX = "collection_index"
DELETION_CURSOR = "cursor"
DELETION_DELETED_COUNTS = "deleted_counts"
DELETION_ATTEMPTS = "attempts"
DELETION_REQUESTED_AT = "requested_at"
DELETION_COMPLETED_AT = "completed_at"
# A completed receipt lingers briefly as proof the subtree really was drained, then
# native TTL reaps it. Long enough to survive a support question, short enough that
# deletion receipts do not become their own retention problem.
DELETION_RECEIPT_TTL_DAYS = 7

# --- run states ----------------------------------------------------------------
STATE_DRAFT = "draft"
STATE_PLANNING = "planning"
STATE_AWAITING_CLARIFICATION = "awaiting_clarification"
STATE_QUEUED = "queued"
STATE_SEARCHING = "searching"
STATE_READING = "reading"
STATE_VERIFYING = "verifying"
STATE_SYNTHESIZING = "synthesizing"
STATE_READY = "ready"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# Absorbing. Search, read, verify and synthesize claims all refuse a run in one of
# these, so a late Cloud Task does no provider work and answers 200. The separately
# typed notify and delete jobs may still act afterwards under their own receipts.
TERMINAL_STATES = (STATE_READY, STATE_PARTIAL, STATE_FAILED, STATE_CANCELLED)
# States in which a stage may legitimately be running.
ACTIVE_STATES = (
    STATE_PLANNING,
    STATE_QUEUED,
    STATE_SEARCHING,
    STATE_READING,
    STATE_VERIFYING,
    STATE_SYNTHESIZING,
)
# STATE_FAILED means ZERO usable evidence and is deliberately rare. Anything holding
# one corroborated claim ends STATE_PARTIAL with named gaps, because a sourced partial
# answer is the product and a bare failure is not.

# Retention lifecycle, deliberately NOT research-result states: a run is deleted or
# not regardless of whether its research succeeded.
DELETION_STATE = "deletion_state"
DELETION_REQUESTED = "deleting"
DELETION_COMPLETE = "deleted"

# --- user-visible progress stages ----------------------------------------------
# Derived from committed state transitions, never an invented percentage.
STAGE_CLASSIFY_PLAN = "classify_plan"
STAGE_SEARCH_WAVE = "search_wave"
STAGE_READ_SOURCE = "read_source"
STAGE_READ_JOIN = "read_join"
STAGE_VERIFY = "verify"
STAGE_SYNTHESIZE = "synthesize"
STAGE_FINALIZE = "finalize"
STAGE_NOTIFY_RESULT = "notify_result"
STAGE_DELETE_RUN = "delete_run"

# --- safe failure codes (a stable enum, NEVER a provider exception string) ------
FAIL_CANCELLED_BY_USER = "cancelled_by_user"
FAIL_CLARIFICATION_TIMEOUT = "clarification_timeout"
FAIL_WALL_CLOCK_EXPIRED = "wall_clock_expired"
FAIL_BUDGET_EXHAUSTED = "budget_exhausted"
FAIL_COST_CAP_REACHED = "cost_cap_reached"
FAIL_ENTITLEMENT_LAPSED = "entitlement_lapsed"
FAIL_METER_UNAVAILABLE = "meter_unavailable"
FAIL_ATTEMPT_CAP = "attempt_cap_exceeded"
FAIL_NO_SOURCE_FOUND = "no_source_found"
FAIL_EXTRACTION_FAILED = "extraction_failed"
FAIL_URL_NOT_ALLOWED = "url_not_allowed"
FAIL_NO_CURRENT_SOURCE = "no_current_source"
FAIL_CONTRADICTORY = "contradictory"
FAIL_ENTITY_BINDING = "entity_binding_unverified"
# Sources were found and they agree, but not of the KIND the policy requires: a
# regulator, a standards body, the primary record, or an independent one. Distinct from
# no_source_found on purpose - "we found nothing" and "we found blogs where the policy
# demands a regulator" are different answers and the user should be told which.
FAIL_SOURCE_POLICY_UNMET = "source_policy_unmet"
FAIL_DEPTH_NOT_AVAILABLE = "depth_not_available"
FAIL_PROVIDER_UNAVAILABLE = "provider_unavailable"

# --- machine codes the desktop client matches on -------------------------------
# Mirrors the existing meeting cap contract shape: an HTTP status plus
# {"detail": {"code": ...}}, which the desktop already parses.
RESEARCH_CAP_CODE = "research_cap_reached"
RESEARCH_PAID_CODE = "research_requires_paid"
RESEARCH_DEPTH_CODE = "depth_not_available"

# --- schema versions -----------------------------------------------------------
RESEARCH_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 2
AUDIT_SCHEMA_VERSION = "research-audit-v1"
POLICY_TABLE_VERSION = "research-policy-v1"

# --- retention -----------------------------------------------------------------
RETENTION_DAYS = 90
# The global domain-class cache expires independently and is never touched by a run
# deletion, because it holds no query, user, or run data.
DOMAIN_CLASS_TTL_DAYS = 30
LIST_LIMIT = 20
