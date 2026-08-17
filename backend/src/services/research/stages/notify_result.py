"""Tell the user their run finished. Runs AFTER the run is result-terminal.

One of the two kinds in ``registry.POST_TERMINAL_KINDS``. That exception exists because
delivery must be able to act on a finished run, and it is safe because this stage carries
its own idempotent receipt and cannot reopen research work: it writes no claim, no source
and no research state.

The notification deliberately carries NO findings. Titles clamp at 120 characters and
bodies at 300, and a research conclusion asserted in 300 characters with no citations
would be the exact opposite of what this pipeline is for. It carries ``resource_id`` so
the client can open the real artifact.

``dedup_key`` includes ``state_revision``, so a redelivery of this stage produces the
same key and the orchestrator drops it as a duplicate. That is what makes the
commit-then-crash recovery path safe: the sweeper may deliver this twice, and the user
still sees one toast.
"""

from __future__ import annotations

from ....lib.logger import logger
from ...notifications import desktop_preferences
from ...notifications.orchestrator import submit
from ...notifications.proposal import (
    SOURCE_RESEARCH,
    DeliveryChannel,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from .. import fields as F
from .base import StageContext, StageResult, StageResultKind

# state -> (notification_type, title, body, severity). Copy is generic by design: the
# subject of a research run can be a medical or financial question, and this text can
# land on a lock screen.
_COPY = {
    F.STATE_READY: (
        "research_ready",
        "Your research is ready",
        "Open Aura to read the sourced brief.",
        "success",
    ),
    F.STATE_PARTIAL: (
        "research_partial",
        "Your research finished with gaps",
        "Open Aura to see what was found and what is missing.",
        "warning",
    ),
    F.STATE_FAILED: (
        "research_failed",
        "Your research could not be completed",
        "Open Aura for details.",
        "error",
    ),
    F.STATE_AWAITING_CLARIFICATION: (
        "research_needs_input",
        "Your research needs one answer",
        "Open Aura to continue.",
        "info",
    ),
}


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports stages.base,
    # so a module-level import here would close a cycle (store -> stages -> registry
    # -> this module -> store) and leave store half-initialised.
    from .. import store

    run_doc = await store.get_run(ctx.uid, ctx.run_id) or {}
    state = str(run_doc.get(F.STATE) or ctx.payload.get("terminal_state") or "")
    revision = int(run_doc.get(F.STATE_REVISION, 0))

    copy = _COPY.get(state)
    if copy is None:
        # A cancelled run gets no toast. The user performed the cancellation; telling
        # them about it is noise, not news.
        return StageResult(
            kind=StageResultKind.DONE,
            stage_outputs={"skipped": True, "state": state},
        )

    notification_type, title, body, severity = copy
    action = (
        "answer_research_question"
        if state == F.STATE_AWAITING_CLARIFICATION
        else "view_research"
    )
    preferences = await desktop_preferences.get(ctx.uid)
    if preferences.research_ui_version < 1 or action not in preferences.supported_actions:
        notification_type = "generic"
        action = "open_notifications"

    decision = await submit(
        NotificationProposal(
            user_id=ctx.uid,
            source=SOURCE_RESEARCH,
            # COMMITTED: the user asked for this and is waiting on it, so it sends inline
            # rather than competing for a proactive slot.
            kind=ProposalKind.COMMITTED,
            dedup_key=f"research:{ctx.run_id}:{state}:{revision}",
            title=title,
            body=body,
            notification_type=notification_type,
            channels=frozenset({DeliveryChannel.DESKTOP}),
            data={
                "severity": severity,
                "toast_policy": "when_hidden",
                "action": action,
                "resource_id": ctx.run_id,
                # Redundant with SENSITIVE_SOURCES, and kept explicit so the intent
                # survives if that set is ever edited.
                "sensitive": "true",
                "notification_origin": SOURCE_RESEARCH,
            },
        )
    )

    duplicate = decision.disposition == Disposition.DROP and decision.reason == "duplicate"
    if duplicate:
        # The proof that a redelivered notify cannot double-toast. Recorded, not raised.
        logger.info(
            "research.notify_result: duplicate suppressed",
            {"run_id": ctx.run_id, "state": state},
        )
    elif decision.disposition != Disposition.SEND:
        # Raising makes the stage retryable under its own attempt cap. The result itself
        # is already durable, so a failed toast never costs the user their brief.
        raise RuntimeError(
            f"notify_result: delivery refused ({decision.disposition}/{decision.reason})"
        )
    elif not getattr(decision, "accepted", False):
        # Transport acceptance is the orchestrator's terminal contract. Device receipt
        # is separate acknowledgement state, and an accepted send keeps its dedup claim;
        # retrying it can only collide with that claim and create a false stage error.
        raise RuntimeError(
            f"notify_result: transport did not accept send ({decision.reason})"
        )

    return StageResult(
        kind=StageResultKind.DONE,
        stage_outputs={
            "state": state,
            "notification_type": notification_type,
            "duplicate": duplicate,
            "delivered": bool(getattr(decision, "delivered", False)),
        },
    )
