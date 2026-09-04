"""Deterministic CaptureRecord -> Notion page write, with a durable receipt.

STEP 3 of the capture firebreak: NO model runs here. The record's typed fields
map to the destination's live schema by structural name equality; anything
that cannot map is dropped and reported, never invented. The receipt at
UserAura/{uid}/notion_writes/{idempotency_key} is what makes a retry converge
instead of double-writing into someone's CRM, and it is written only AFTER
Notion confirms the page (write the cache after the side effect succeeds).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..notion_connector import NotionConnector
from .extract import CaptureRecord

RECEIPTS_ROOT = "UserAura"
RECEIPTS_SUBCOLLECTION = "notion_writes"

_RICH_TEXT_LIMIT = 2000
_MAX_RETRY_AFTER_S = 5.0


@dataclass(frozen=True, slots=True)
class WriteResult:
    ok: bool
    page_id: str | None = None
    page_url: str | None = None
    database_name: str | None = None
    dropped_fields: list[str] = field(default_factory=list)
    already_saved: bool = False
    error: str | None = None


def _receipt_ref(uid: str, idempotency_key: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection(RECEIPTS_ROOT)
        .document(uid)
        .collection(RECEIPTS_SUBCOLLECTION)
        .document(idempotency_key)
    )


def _normalized(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _text_value(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": value[:_RICH_TEXT_LIMIT]}}]


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _text_value(text)},
    }


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _text_value(text)},
    }


def map_record_to_page(
    record: CaptureRecord,
    schema: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Pure mapping: (properties, children blocks, dropped field names).

    Structural matching only: an extracted fact lands in a property whose
    normalized name equals the fact's normalized name and whose type accepts
    text or a URL. Everything else becomes page body or is dropped and named.
    """
    properties: dict[str, Any] = {}
    dropped: list[str] = []
    schema_by_normalized = {_normalized(name): (name, ptype) for name, ptype in schema.items()}

    title_property = next((name for name, ptype in schema.items() if ptype == "title"), None)
    if title_property:
        properties[title_property] = {"title": _text_value(record.title)}
    else:
        dropped.append("title")

    url_property = next((name for name, ptype in schema.items() if ptype == "url"), None)
    if record.source_url:
        if url_property:
            properties[url_property] = {"url": record.source_url[:2000]}
        else:
            dropped.append("source_url")

    body_facts: list[str] = []
    for fact in record.key_facts:
        matched = schema_by_normalized.get(_normalized(fact.name))
        if matched:
            name, ptype = matched
            if name in properties:
                body_facts.append(f"{fact.name}: {fact.value}")
                continue
            if ptype == "rich_text":
                properties[name] = {"rich_text": _text_value(fact.value)}
                continue
            if ptype == "url":
                properties[name] = {"url": fact.value[:2000]}
                continue
            if ptype == "number":
                try:
                    properties[name] = {"number": float(fact.value.replace(",", ""))}
                    continue
                except ValueError:
                    pass
            # Select/relation/date and friends need values that exist in the
            # schema's option space; guessing invents data, so those facts go
            # to the body instead.
        body_facts.append(f"{fact.name}: {fact.value}")

    children: list[dict[str, Any]] = []
    if record.summary:
        children.append(_paragraph(record.summary))
    for line in body_facts:
        children.append(_paragraph(line))
    if record.body_text:
        children.append(_paragraph(record.body_text))
    if record.source_app:
        children.append(_paragraph(f"Captured from: {record.source_app}"))

    return properties, children, dropped


def _create_page(
    connector: NotionConnector,
    *,
    data_source_id: str,
    properties: dict[str, Any],
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    }
    if children:
        body["children"] = children

    response = connector.authorized_request("POST", "/v1/pages", json_body=body)
    if response.status_code == 429:
        retry_after = min(
            float(response.headers.get("Retry-After") or 1.0), _MAX_RETRY_AFTER_S
        )
        time.sleep(retry_after)
        response = connector.authorized_request("POST", "/v1/pages", json_body=body)
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("code") or "")
        except Exception:
            pass
        raise ValueError(f"Notion page create failed ({response.status_code}): {detail}")
    return response.json()


async def write_capture(
    *,
    uid: str,
    data_source_id: str,
    database_name: str,
    record: CaptureRecord,
    idempotency_key: str,
    session_id: str,
    schema: dict[str, str],
) -> WriteResult:
    """One record into one bound data source; retries converge on the receipt."""
    ref = _receipt_ref(uid, idempotency_key)
    existing = await asyncio.to_thread(lambda: ref.get().to_dict())
    if existing and not existing.get("undone_at"):
        return WriteResult(
            ok=True,
            page_id=existing.get("page_id"),
            page_url=existing.get("page_url"),
            database_name=existing.get("database_name") or database_name,
            already_saved=True,
        )

    properties, children, dropped = map_record_to_page(record, schema)
    connector = NotionConnector(uid)
    page = await asyncio.to_thread(
        _create_page,
        connector,
        data_source_id=data_source_id,
        properties=properties,
        children=children,
    )
    page_id = str(page.get("id") or "")
    page_url = str(page.get("url") or "")

    # Receipt AFTER the side effect succeeds. If this write fails, the page
    # exists without a receipt and the same key retried finds no receipt and
    # would double-write — so a receipt failure is reported as a failure even
    # though the page landed; the spoken result must not claim "saved".
    try:
        await asyncio.to_thread(
            ref.set,
            {
                "data_source_id": data_source_id,
                "database_name": database_name,
                "page_id": page_id,
                "page_url": page_url,
                "idempotency_key": idempotency_key,
                "session_id": session_id,
                "created_at": datetime.now(UTC).isoformat(),
                "undone_at": None,
            },
        )
    except Exception as exc:
        logger.error(
            "notion.write: receipt write failed after page create",
            {"user_id": uid, "page_id": page_id, "error": str(exc)},
        )
        return WriteResult(ok=False, error="receipt_failed", dropped_fields=dropped)

    logger.info(
        "notion.write: page created",
        {
            "user_id": uid,
            "session_id": session_id,
            "page_id": page_id,
            "dropped_field_count": len(dropped),
        },
    )
    return WriteResult(
        ok=True,
        page_id=page_id,
        page_url=page_url,
        database_name=database_name,
        dropped_fields=dropped,
    )


# Notion caps a create at 1000 blocks; a brief is single-digit KB but the cap
# is enforced anyway so a pathological run degrades to a truncated page, never
# a failed delivery.
_MAX_BRIEF_BLOCKS = 950


def map_brief_to_blocks(request_text: str, brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure mapping from a synthesized research brief to Notion child blocks.

    Deterministic and model-free: statement text is already claim-qualified by
    the synthesize stage, and nothing here rephrases, summarizes, or invents.
    """
    children: list[dict[str, Any]] = []
    summary = str(brief.get("executive_summary") or "").strip()
    if summary:
        children.append(_paragraph(summary))
    for section in brief.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        if heading:
            children.append(_heading(heading))
        for statement in section.get("statements") or []:
            if not isinstance(statement, dict):
                continue
            text = str(statement.get("text") or "").strip()
            if text:
                children.append(_paragraph(text))
    gaps = [gap for gap in (brief.get("gaps") or []) if isinstance(gap, dict)]
    if gaps:
        children.append(_heading("Gaps"))
        for gap in gaps:
            detail = str(gap.get("detail") or gap.get("reason") or "").strip()
            if detail:
                children.append(_paragraph(detail))
    disagreements = [str(item) for item in (brief.get("disagreements") or []) if str(item).strip()]
    if disagreements:
        children.append(_heading("Disagreements"))
        for item in disagreements:
            children.append(_paragraph(item))
    for disclaimer in brief.get("disclaimers") or []:
        text = str(disclaimer).strip()
        if text:
            children.append(_paragraph(text))
    if not children:
        children.append(_paragraph(f"Research request: {request_text}".strip()))
    return children[:_MAX_BRIEF_BLOCKS]


def research_delivery_key(uid: str, run_id: str) -> str:
    """Stable receipt key for one run's Notion delivery; retries converge."""
    material = "\x1f".join((uid, run_id, "notion_deliver"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def write_research_brief(
    *,
    uid: str,
    data_source_id: str,
    database_name: str,
    request_text: str,
    brief: dict[str, Any],
    run_id: str,
    schema: dict[str, str],
) -> WriteResult:
    """One research brief into the bound data source as ONE page.

    Same receipt store and ordering discipline as write_capture: receipt-first
    idempotency, page create, receipt only after Notion confirms.
    """
    idempotency_key = research_delivery_key(uid, run_id)
    ref = _receipt_ref(uid, idempotency_key)
    existing = await asyncio.to_thread(lambda: ref.get().to_dict())
    if existing and not existing.get("undone_at"):
        return WriteResult(
            ok=True,
            page_id=existing.get("page_id"),
            page_url=existing.get("page_url"),
            database_name=existing.get("database_name") or database_name,
            already_saved=True,
        )

    properties: dict[str, Any] = {}
    title_property = next((name for name, ptype in schema.items() if ptype == "title"), None)
    title_text = " ".join((request_text or "Research brief").split())[:200]
    if title_property:
        properties[title_property] = {"title": _text_value(title_text)}
    children = map_brief_to_blocks(request_text, brief)

    connector = NotionConnector(uid)
    page = await asyncio.to_thread(
        _create_page,
        connector,
        data_source_id=data_source_id,
        properties=properties,
        children=children,
    )
    page_id = str(page.get("id") or "")
    page_url = str(page.get("url") or "")

    try:
        await asyncio.to_thread(
            ref.set,
            {
                "data_source_id": data_source_id,
                "database_name": database_name,
                "page_id": page_id,
                "page_url": page_url,
                "idempotency_key": idempotency_key,
                "session_id": f"research:{run_id}",
                "created_at": datetime.now(UTC).isoformat(),
                "undone_at": None,
            },
        )
    except Exception as exc:
        logger.error(
            "notion.write: research receipt write failed after page create",
            {"user_id": uid, "run_id": run_id, "page_id": page_id, "error": str(exc)},
        )
        return WriteResult(ok=False, error="receipt_failed")

    logger.info(
        "notion.write: research brief delivered",
        {"user_id": uid, "run_id": run_id, "page_id": page_id, "block_count": len(children)},
    )
    return WriteResult(
        ok=True,
        page_id=page_id,
        page_url=page_url,
        database_name=database_name,
    )


def _create_database_sync(connector: NotionConnector, name: str) -> tuple[str, str]:
    """Create a workspace-level database with a deliberately generic schema.

    The schema is fixed (Name/Source/Notes) rather than derived from extracted
    screen content: screen-derived property names would let untrusted content
    shape the user's workspace structure. Facts land in the page body instead.
    Returns (data_source_id, database_name).
    """
    body = {
        "parent": {"type": "workspace", "workspace": True},
        "title": [{"type": "text", "text": {"content": name[:200]}}],
        "initial_data_source": {
            "properties": {
                "Name": {"title": {}},
                "Source": {"url": {}},
                "Notes": {"rich_text": {}},
            }
        },
    }
    response = connector.authorized_request("POST", "/v1/databases", json_body=body)
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("code") or "")
        except Exception:
            pass
        raise ValueError(f"Notion database create failed ({response.status_code}): {detail}")
    payload = response.json()
    data_sources = payload.get("data_sources") or []
    if not data_sources or not data_sources[0].get("id"):
        raise ValueError("Notion database create returned no data source")
    return str(data_sources[0]["id"]), name


async def create_database(*, uid: str, name: str) -> tuple[str, str]:
    """Voice-confirmed database creation; name comes from the user's own words."""
    cleaned = " ".join((name or "").split())[:200]
    if not cleaned:
        raise ValueError("database name is required")
    return await asyncio.to_thread(_create_database_sync, NotionConnector(uid), cleaned)


async def undo_write(*, uid: str, idempotency_key: str) -> bool:
    """Archive the written page (Notion archives, so undo is reversible)."""
    ref = _receipt_ref(uid, idempotency_key)
    receipt = await asyncio.to_thread(lambda: ref.get().to_dict())
    if not receipt or receipt.get("undone_at"):
        return False
    page_id = receipt.get("page_id")
    if not page_id:
        return False

    connector = NotionConnector(uid)
    response = await asyncio.to_thread(
        connector.authorized_request,
        "PATCH",
        f"/v1/pages/{page_id}",
        json_body={"archived": True},
    )
    if response.status_code != 200:
        logger.warn(
            "notion.write: undo archive failed",
            {"user_id": uid, "page_id": page_id, "status": response.status_code},
        )
        return False
    await asyncio.to_thread(
        ref.set, {"undone_at": datetime.now(UTC).isoformat()}, merge=True
    )
    return True
