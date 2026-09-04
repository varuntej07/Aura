"""Write a finished run's brief into the user's bound Notion data source.

One of the kinds in ``registry.POST_TERMINAL_KINDS``: it acts on a
result-terminal run under its own idempotent receipt and can never reopen
research work. Deterministic and model-free by construction - the section 3
firebreak's third rule. The destination was bound from the user's own words
before dispatch and is immutable on the run doc; nothing researched from the
web can influence WHERE this writes, only the inert text of the page.

Failure philosophy mirrors notify_result: the brief is already durable, so a
failed delivery must never cost the user their result. Transient Notion
errors raise (retry under the stage attempt cap); a dead token or a missing
delivery config completes the stage with a failed receipt instead, because
retrying cannot fix either. Either way this stage enqueues notify_result as
its successor, so the ONE notification the user gets states what actually
happened, from the receipt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ....lib.logger import logger
from ...notion.write import write_research_brief
from ...notion_connector import NotionReauthorizationRequired
from .. import fields as F
from .base import NextJob, StageContext, StageResult, StageResultKind


def _result(
    ctx: StageContext,
    *,
    delivery_result: dict[str, object],
    outputs: dict[str, object],
) -> StageResult:
    return StageResult(
        kind=StageResultKind.DONE,
        next_jobs=(
            NextJob(
                stage_kind=F.STAGE_NOTIFY_RESULT,
                wave=ctx.wave,
                payload={"terminal_state": str(ctx.payload.get("terminal_state") or "")},
            ),
        ),
        run_updates={F.DELIVERY_RESULT: delivery_result},
        stage_outputs=outputs,
    )


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports
    # stages.base, so a module-level import would close the
    # store -> stages -> registry -> this module -> store cycle.
    from .. import store

    run_doc = await store.get_run(ctx.uid, ctx.run_id) or {}
    delivery = dict(run_doc.get(F.DELIVERY) or {})
    data_source_id = str(delivery.get("data_source_id") or "")
    database_name = str(delivery.get("database_name") or "")
    if not data_source_id:
        # finalize only routes here when DELIVERY exists, so this is drift,
        # not a user outcome. Complete with a failed receipt; retrying cannot
        # conjure a destination.
        logger.error(
            "research.notion_deliver: run has no delivery config",
            {"run_id": ctx.run_id},
        )
        return _result(
            ctx,
            delivery_result={"failed": F.FAIL_DELIVERY_FAILED, "database_name": database_name},
            outputs={"delivered": False, "reason": "missing_delivery_config"},
        )

    if await ctx.is_cancelled():
        return _result(
            ctx,
            delivery_result={"failed": F.FAIL_CANCELLED_BY_USER, "database_name": database_name},
            outputs={"delivered": False, "reason": "cancelled"},
        )

    brief = dict(run_doc.get(F.BRIEF) or {})
    request_text = str(run_doc.get(F.REQUEST_TEXT) or "")

    try:
        from ...notion.schema import data_source_schema

        schema = await data_source_schema(ctx.uid, data_source_id)
        write_result = await write_research_brief(
            uid=ctx.uid,
            data_source_id=data_source_id,
            database_name=database_name,
            request_text=request_text,
            brief=brief,
            run_id=ctx.run_id,
            schema=schema,
        )
    except NotionReauthorizationRequired:
        # The stored credentials are dead; only the user can mint new ones, so
        # a retry burns an attempt for nothing. Complete with the honest code.
        logger.warn(
            "research.notion_deliver: delivery blocked on reauthorization",
            {"user_id": ctx.uid, "run_id": ctx.run_id},
        )
        return _result(
            ctx,
            delivery_result={"failed": F.FAIL_DELIVERY_REAUTH, "database_name": database_name},
            outputs={"delivered": False, "reason": "reauthorization_required"},
        )
    # Anything else (Notion 5xx, network, schema fetch failure) propagates:
    # the engine retries under STAGE_ATTEMPT_CAP, and the receipt-first
    # idempotency inside write_research_brief makes the retry converge on one
    # page. At the cap, fail_stage's terminal notify path reports honestly.

    if not write_result.ok:
        # Page may exist without its receipt (receipt_failed). Raising lets
        # the retry find the receipt path again; write_research_brief re-reads
        # the receipt first, so a converged retry returns already_saved.
        raise RuntimeError(f"notion_deliver: write failed ({write_result.error})")

    return _result(
        ctx,
        delivery_result={
            "page_id": write_result.page_id,
            "page_url": write_result.page_url,
            "database_name": write_result.database_name or database_name,
            "delivered_at": datetime.now(UTC).isoformat(),
            "already_saved": write_result.already_saved,
        },
        outputs={
            "delivered": True,
            "already_saved": write_result.already_saved,
        },
    )
