"""Schema contract for the per-user daily cost doc: ``users/{uid}/cost/{YYYY-MM-DD}``.

One doc, two writers, one reader today:
  - services/reactive/cost_cap.py increments ``llm_calls`` (the runaway breaker)
    and reads it back against the daily ceiling. Since 2026-08-31 it counts REAL
    provider attempts made inside the agent dispatch loop (via model_provider's
    capture_usage), not dispatched agents: the old proxy over-counted ~83x.
  - services/analytics/llm_cost_ledger.py merge-increments the per-user LLM
    spend ledger fields (generations, tokens, estimated microUSD).

Both reference these constants, never string literals, so the writer and the
reader of any field can never silently drift apart.
"""

from __future__ import annotations

from datetime import timedelta

# Generic layer-wide names re-exported here so cost-doc consumers have a single
# import site for the whole doc schema.
from .reactive.fields import FIELD_EXPIRES_AT, USERS_COLLECTION

__all__ = [
    "COST_SUBCOLLECTION",
    "COST_DOC_TTL",
    "FIELD_LLM_CALLS",
    "FIELD_LLM_GENERATIONS",
    "FIELD_LLM_INPUT_TOKENS",
    "FIELD_LLM_CACHED_INPUT_TOKENS",
    "FIELD_LLM_OUTPUT_TOKENS",
    "FIELD_EST_LLM_MICROUSD",
    "FIELD_EXPIRES_AT",
    "USERS_COLLECTION",
]

COST_SUBCOLLECTION = "cost"

# The cap only ever reads today's doc, but since 2026-08 the same daily doc also
# carries the per-user LLM spend ledger (services/analytics/llm_cost_ledger.py):
# tokens and estimated µUSD per day. Spend history is only useful if it survives,
# so retention is 90 days rather than the original 3. Storage cost is one tiny
# doc per active user per day; native TTL still reaps via expires_at.
COST_DOC_TTL = timedelta(days=90)

# Reactive cost-cap counter (the soft per-day LLM-call ceiling).
#
# DELIBERATELY NOT the same number as FIELD_LLM_GENERATIONS below, even though both live
# on this doc and both now count real provider attempts. This one is scoped to REACTIVE
# agent work only; llm_generations counts every LLM call the user causes, chat included.
# Pointing the reactive breaker at llm_generations would let a heavy chat day exhaust
# DAILY_LLM_CALL_CAP and silently suppress that user's icebreakers and thread follow-ups.
# A runaway breaker must be scoped to the loop it is breaking.
FIELD_LLM_CALLS = "llm_calls"

# Spend-ledger fields (merge-incremented per provider API attempt).
FIELD_LLM_GENERATIONS = "llm_generations"
FIELD_LLM_INPUT_TOKENS = "llm_input_tokens"
FIELD_LLM_CACHED_INPUT_TOKENS = "llm_cached_input_tokens"
FIELD_LLM_OUTPUT_TOKENS = "llm_output_tokens"
FIELD_EST_LLM_MICROUSD = "est_llm_microusd"
