"""Deterministic client_run_id minting, shared by every research dispatcher.

One helper instead of per-caller copies, because the copies collided: the
voice worker's research_to_notion and tool_executor's start_research each
derived "voice:{session_id}:{sha256(request)[:16]}" independently, producing
byte-identical ids for the same request text in one session. create_run's
replay branch then returned the first tool's run and silently dropped the
second tool's delivery binding — Buddy claimed "I'll save it to X" while no
notion_deliver stage existed.

The tool name is therefore part of the identity: the same words spoken to two
different tools are two different runs by construction, while an identical
retry of the same tool call still replays (the property the digest exists
for).
"""

from __future__ import annotations

import hashlib
from uuid import uuid4


def client_run_id_for(*, scope: str, tool_name: str, request_text: str) -> str:
    """Deterministic run identity: constant per (scope, tool, request).

    scope: the caller's stable per-conversation prefix ("voice:{session_id}"
    on voice, the client message id on chat). Must be non-empty; callers
    without a stable scope should use random_client_run_id().
    """
    digest = hashlib.sha256(request_text.casefold().encode("utf-8")).hexdigest()[:16]
    return f"{scope}:{tool_name}:{digest}"


def retry_salted(client_run_id: str) -> str:
    """A fresh identity for restarting when the deterministic id points at a
    dead or wrongly-bound run; the nonce guarantees a genuinely new run doc."""
    return f"{client_run_id}:retry:{uuid4()}"


def random_client_run_id() -> str:
    return str(uuid4())
