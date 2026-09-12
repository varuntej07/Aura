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
from ...notion.write import (
    NotionRequestRejected,
    create_database_for_run,
    write_research_brief,
    write_research_rows,
)
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
    create_named = str(delivery.get("create_database_named") or "")
    # Read before the create below, because a table answer decides the schema of
    # the database we are about to make.
    brief = dict(run_doc.get(F.BRIEF) or {})
    request_text = str(run_doc.get(F.REQUEST_TEXT) or "")
    rows = list(brief.get("rows") or [])
    columns = list(dict(brief.get("table") or {}).get("columns") or [])
    if not data_source_id and create_named:
        # The user named a database that did not exist when they said it. It is
        # created HERE rather than at bind time so nothing is added to their
        # workspace for a run that later failed, and so the create carries a
        # receipt: the binding on the run stays immutable either way.
        try:
            data_source_id, database_name = await create_database_for_run(
                uid=ctx.uid,
                run_id=ctx.run_id,
                name=create_named,
                columns=tuple(columns) if rows else (),
            )
        except NotionReauthorizationRequired:
            logger.warn(
                "research.notion_deliver: database create blocked on reauthorization",
                {"user_id": ctx.uid, "run_id": ctx.run_id},
            )
            return _result(
                ctx,
                delivery_result={
                    "failed": F.FAIL_DELIVERY_REAUTH,
                    "database_name": create_named,
                },
                outputs={"delivered": False, "reason": "reauthorization_required"},
            )
        except NotionRequestRejected as exc:
            logger.error(
                "research.notion_deliver: database create rejected as invalid",
                {
                    "user_id": ctx.uid,
                    "run_id": ctx.run_id,
                    "status": exc.status_code,
                    "code": exc.code,
                },
            )
            return _result(
                ctx,
                delivery_result={
                    "failed": F.FAIL_DELIVERY_REJECTED,
                    "database_name": create_named,
                },
                outputs={"delivered": False, "reason": "database_create_rejected"},
            )
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

    try:
        from ...notion.schema import data_source_schema

        schema = await data_source_schema(ctx.uid, data_source_id)
        if rows and columns:
            # A table answer: one page per row, mapped onto whatever columns the
            # destination actually has. The narrative brief stays in the app; a
            # summary written as a row would be a fake entity in the user's own
            # data, breaking every sort and filter over it.
            rows_written, rows_failed = await write_research_rows(
                uid=ctx.uid,
                data_source_id=data_source_id,
                rows=rows,
                columns=columns,
                run_id=ctx.run_id,
                schema=schema,
            )
            if rows_written == 0:
                # Nothing landed. Raising retries under the attempt cap rather
                # than recording a delivery that did not happen.
                raise RuntimeError("notion_deliver: no rows written")
            return _result(
                ctx,
                delivery_result={
                    "database_name": database_name,
                    "rows_written": rows_written,
                    "rows_failed": rows_failed,
                    "partial": bool(rows_failed),
                    "delivered_at": datetime.now(UTC).isoformat(),
                },
                outputs={
                    "delivered": True,
                    "rows_written": rows_written,
                    "rows_failed": rows_failed,
                },
            )
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
    except NotionRequestRejected as exc:
        # Notion rejected the body as invalid (schema mapping drift, payload
        # cap). Permanent by definition: the identical retry would burn the
        # attempt cap on the identical 400. Complete with a distinct code so
        # the audit trail separates "our request was wrong" from "Notion was
        # down".
        logger.error(
            "research.notion_deliver: delivery rejected as invalid",
            {
                "user_id": ctx.uid,
                "run_id": ctx.run_id,
                "status": exc.status_code,
                "code": exc.code,
            },
        )
        return _result(
            ctx,
            delivery_result={
                "failed": F.FAIL_DELIVERY_REJECTED,
                "database_name": database_name,
            },
            outputs={"delivered": False, "reason": "request_rejected", "code": exc.code},
        )
    # Anything else (Notion 5xx, network, schema fetch failure) propagates:
    # the engine retries under STAGE_ATTEMPT_CAP, and the receipt-first
    # idempotency inside write_research_brief makes the retry converge on one
    # page. At the cap, fail_stage's terminal notify path reports honestly.

    if not write_result.ok and write_result.error == "receipt_failed":
        if ctx.attempt >= store.STAGE_ATTEMPT_CAP:
            # Final attempt: the page provably landed (create succeeded) but
            # its receipt write failed every time. Raising again would report
            # "saving failed" over a real page in the user's database — a lie
            # in the harmful direction. Record the honest middle state; the
            # notification says saved-with-caveat instead of failure.
            logger.error(
                "research.notion_deliver: page delivered but receipt "
                "unrecordable at attempt cap",
                {"user_id": ctx.uid, "run_id": ctx.run_id, "attempt": ctx.attempt},
            )
            return _result(
                ctx,
                delivery_result={
                    "delivered_unreceipted": True,
                    "database_name": database_name,
                    "delivered_at": datetime.now(UTC).isoformat(),
                },
                outputs={"delivered": True, "receipted": False},
            )
        # Page may exist without its receipt. Raising lets the retry find the
        # receipt path again; write_research_brief re-reads the receipt first,
        # so a converged retry returns already_saved.
        raise RuntimeError(f"notion_deliver: write failed ({write_result.error})")
    if not write_result.ok:
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
