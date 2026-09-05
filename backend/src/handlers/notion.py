"""POST /notion/{resolve,write,undo} — the voice worker's Notion capture routes.

Guarded by the same Firebase ID-token check as /screen-saves (require_user):
the voice worker authenticates with a per-session Firebase ID token minted for
the session's own user (agent/voice/auth.py), so these routes can only ever
act for that user. Deliberately NOT under /internal/*, which is the
Cloud-Tasks/OIDC namespace.

Firebreak note: these routes never see screen bytes. Extraction happens
worker-side; /notion/write receives an already-typed CaptureRecord, and
/notion/resolve receives only the user's spoken destination words.

Error discipline: every failure returns a distinct machine code (the worker
picks the spoken line from it), logs with the idempotency key where one
exists, and — for the write/undo paths, which carry a key — leaves a durable
failure record in Firestore via record_write_failure so a root cause never
requires Cloud Logging alone.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ..lib.logger import logger
from ..services.notion.extract import CaptureRecord
from ..services.notion.resolve import invalidate_cache, resolve_destination
from ..services.notion.schema import data_source_schema, invalidate_schema_cache
from ..services.notion.write import (
    NotionRequestRejected,
    ReceiptCheckFailed,
    create_database,
    record_write_failure,
    undo_write,
    write_capture,
)
from ..services.notion_connector import NotionReauthorizationRequired
from .request_guards import require_user


class ResolveBody(BaseModel):
    spoken_destination: str = Field(min_length=1, max_length=300)


class WriteBody(BaseModel):
    data_source_id: str = Field(min_length=1, max_length=64)
    database_name: str = Field(min_length=1, max_length=300)
    record: CaptureRecord
    idempotency_key: str = Field(min_length=32, max_length=32)
    session_id: str = Field(min_length=1, max_length=128)


class UndoBody(BaseModel):
    idempotency_key: str = Field(min_length=32, max_length=32)


class CreateDatabaseBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _reauthorization_required() -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "reauthorization_required"})


async def handle_notion_resolve(request: Request) -> JSONResponse:
    user_id = require_user(request)
    try:
        body = ResolveBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "spoken_destination is required."})

    try:
        resolved = await resolve_destination(user_id, body.spoken_destination)
    except NotionReauthorizationRequired:
        return _reauthorization_required()
    except Exception as exc:
        logger.warn(
            "Notion: resolve failed", {"user_id": user_id, "error": str(exc)}
        )
        return JSONResponse(status_code=502, content={"error": "resolve_failed"})

    schema: dict[str, str] = {}
    if resolved.outcome == "bind" and resolved.data_source_id:
        try:
            schema = await data_source_schema(user_id, resolved.data_source_id)
        except NotionReauthorizationRequired:
            return _reauthorization_required()
        except Exception as exc:
            logger.warn(
                "Notion: schema fetch failed",
                {"user_id": user_id, "error": str(exc)},
            )
            return JSONResponse(status_code=502, content={"error": "schema_failed"})

    return JSONResponse(
        status_code=200,
        content={
            "outcome": resolved.outcome,
            "data_source_id": resolved.data_source_id,
            "title": resolved.title,
            "confidence": resolved.confidence,
            "candidates": [asdict(candidate) for candidate in resolved.candidates],
            "schema": schema,
        },
    )


async def handle_notion_write(request: Request) -> JSONResponse:
    user_id = require_user(request)
    try:
        body = WriteBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid_write_body"})

    async def _fail(
        stage: str, error_code: str, *, http_status: int | None = None, detail: str = ""
    ) -> JSONResponse:
        await record_write_failure(
            uid=user_id,
            idempotency_key=body.idempotency_key,
            stage=stage,
            error_code=error_code,
            http_status=http_status,
            detail=detail,
        )
        return JSONResponse(status_code=502, content={"error": error_code})

    # Schema fetch and page write are separate failure surfaces on purpose: a
    # schema failure guarantees no page exists; after a write failure one might.
    try:
        schema = await data_source_schema(user_id, body.data_source_id)
    except NotionReauthorizationRequired:
        return _reauthorization_required()
    except Exception as exc:
        logger.warn(
            "Notion: write-path schema fetch failed",
            {
                "user_id": user_id,
                "idempotency_key": body.idempotency_key,
                "error": str(exc),
            },
        )
        return await _fail("schema", "schema_failed", detail=str(exc))

    try:
        result = await write_capture(
            uid=user_id,
            data_source_id=body.data_source_id,
            database_name=body.database_name,
            record=body.record,
            idempotency_key=body.idempotency_key,
            session_id=body.session_id,
            schema=schema,
        )
    except NotionReauthorizationRequired:
        return _reauthorization_required()
    except ReceiptCheckFailed as exc:
        # The write never started; retrying is unconditionally safe.
        return await _fail("receipt_check", "receipt_check_failed", detail=str(exc))
    except NotionRequestRejected as exc:
        logger.error(
            "Notion: write rejected as invalid",
            {
                "user_id": user_id,
                "idempotency_key": body.idempotency_key,
                "status": exc.status_code,
                "code": exc.code,
            },
        )
        return await _fail(
            "page_create", "write_rejected", http_status=exc.status_code, detail=exc.code
        )
    except Exception as exc:
        logger.warn(
            "Notion: write failed",
            {
                "user_id": user_id,
                "idempotency_key": body.idempotency_key,
                "error": str(exc),
            },
        )
        return await _fail("page_create", "write_failed", detail=str(exc))

    if not result.ok:
        return await _fail("receipt", result.error or "write_failed")
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "page_id": result.page_id,
            "page_url": result.page_url,
            "database_name": result.database_name,
            "dropped_fields": result.dropped_fields,
            "already_saved": result.already_saved,
        },
    )


async def handle_notion_create_database(request: Request) -> JSONResponse:
    """Voice-confirmed database creation. The name is the user's own words
    (never screen-derived); the schema is fixed inside create_database."""
    user_id = require_user(request)
    try:
        body = CreateDatabaseBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "name is required."})

    try:
        data_source_id, database_name = await create_database(uid=user_id, name=body.name)
    except NotionReauthorizationRequired:
        return _reauthorization_required()
    except Exception as exc:
        logger.warn(
            "Notion: database create failed", {"user_id": user_id, "error": str(exc)}
        )
        return JSONResponse(status_code=502, content={"error": "create_failed"})

    # The new database must be findable next resolve, not 15 minutes from now,
    # and no stale schema may shadow the fresh one.
    invalidate_cache(user_id)
    invalidate_schema_cache(user_id)
    return JSONResponse(
        status_code=200,
        content={"data_source_id": data_source_id, "database_name": database_name},
    )


async def handle_notion_undo(request: Request) -> JSONResponse:
    user_id = require_user(request)
    try:
        body = UndoBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "idempotency_key is required."})

    try:
        undone = await undo_write(uid=user_id, idempotency_key=body.idempotency_key)
    except NotionReauthorizationRequired:
        return _reauthorization_required()
    except Exception as exc:
        logger.warn(
            "Notion: undo failed",
            {
                "user_id": user_id,
                "idempotency_key": body.idempotency_key,
                "error": str(exc),
            },
        )
        await record_write_failure(
            uid=user_id,
            idempotency_key=body.idempotency_key,
            stage="undo",
            error_code="undo_failed",
            detail=str(exc),
        )
        return JSONResponse(status_code=502, content={"error": "undo_failed"})
    return JSONResponse(status_code=200, content={"ok": undone})
