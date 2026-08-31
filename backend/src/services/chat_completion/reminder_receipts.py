"""Reminder receipt shaping for the chat "done" metadata payload.

Lives beside its only consumers (handlers/chat.py and completion.py), which
both decide whether a turn's reminder receipt should reach the client UI.
"""

from __future__ import annotations

from typing import Any

from ...shared.tools import REMINDER_RECEIPT_EXISTING


def reminder_ui_payload(
    receipt: Any, tool_names: list[str] | tuple[str, ...]
) -> dict[str, Any] | None:
    """Return a reminder receipt only when this turn changed reminder state.

    Deduplication is a successful tool outcome for the model, but an unchanged
    pre-existing reminder is not a newly-created UI artifact.  Keeping this
    distinction structural prevents an old receipt from replacing an unrelated
    answer without inspecting the user's words or the model's prose.
    """
    if not isinstance(receipt, dict):
        return None
    raw_status = receipt.get("receipt_status")
    if raw_status is None:
        # Canonical transcripts written before receipt outcomes existed are
        # already trusted creation artifacts. Preserve their exact shape so old
        # sessions keep hydrating without a migration.
        return dict(receipt)
    status = str(raw_status)
    if status == REMINDER_RECEIPT_EXISTING:
        return None
    payload = dict(receipt)
    payload["receipt_status"] = status
    payload["display_mode"] = (
        "supplemental"
        if any(name != "set_reminder" for name in tool_names)
        else "standalone"
    )
    return payload
