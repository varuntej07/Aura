"""Canonical semantic privacy gate for unsolicited thread outreach."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..memory import graph_fields as GF
from ..model_provider import get_model_provider

if TYPE_CHECKING:
    from ..notifications.proposal import NotificationProposal

SENSITIVE_CATEGORY_SLUGS = frozenset({
    "health_medical",
    "personal_finance",
    "relationships_social",
    "law_legal",
    "gender_identity",
    "grief_loss",
    "trauma_abuse",
})

_CLASSIFIER_TIMEOUT_S = 6.0
_SYSTEM_PROMPT = """You are Aura's privacy classifier for UNSOLICITED proactive outreach.
Classify the subject semantically, not by matching words. Sensitive includes health and physical
symptoms, medication or dosing, gender or identity, grief, intimate or family relationships,
personal finance or employment distress, legal trouble, trauma, abuse, sexuality, and any similarly
private subject that could embarrass, expose, pressure, or harm someone if surfaced unexpectedly.
Ordinary hobbies, games, public media, schoolwork, and routine projects are not sensitive unless the
context makes them private. When the text is vague and could plausibly be private, set
plausibly_sensitive=true. Do not infer that user interest or prior discussion grants permission for
unsolicited outreach. Return only the requested structured result."""


class _Judgment(BaseModel):
    sensitive: bool
    plausibly_sensitive: bool = False
    categories: list[str] = Field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class SensitivityDecision:
    status: str
    categories: list[str]
    source: str
    reason: str
    checked_at: str

    @property
    def allows_proactive(self) -> bool:
        return self.status == "clear"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decision_from_dict(value: Any) -> SensitivityDecision | None:
    if not isinstance(value, dict):
        return None
    try:
        return SensitivityDecision(
            status=str(value.get("status") or "unknown"),
            categories=[str(item) for item in value.get("categories") or []],
            source=str(value.get("source") or "legacy"),
            reason=str(value.get("reason") or ""),
            checked_at=str(value.get("checked_at") or ""),
        )
    except (TypeError, ValueError):
        return None


def _decision(status: str, categories: list[str], source: str, reason: str) -> SensitivityDecision:
    return SensitivityDecision(
        status=status,
        categories=list(dict.fromkeys(categories))[:4],
        source=source,
        reason=reason[:120],
        checked_at=datetime.now(UTC).isoformat(),
    )


async def read_graph_sensitivity_nodes(
    user_id: str, entity_keys: list[str]
) -> list[dict[str, Any]]:
    """Read the canonical graph flags for a topic's persisted semantic identities."""
    node_ids = [GF.entity_id(str(key)) for key in entity_keys if str(key)]
    if not node_ids:
        return []

    def _read() -> list[dict[str, Any]]:
        db = admin_firestore()
        collection = (
            db.collection(GF.PARENT_COLLECTION)
            .document(user_id)
            .collection(GF.NODE_SUBCOLLECTION)
        )
        return [
            snap.to_dict() or {}
            for snap in db.get_all([collection.document(node_id) for node_id in node_ids])
            if snap.exists
        ]

    return await asyncio.to_thread(_read)


async def classify_proactive_subject(
    text: str,
    *,
    category: str | None = None,
    explicit_sensitive: bool = False,
    graph_nodes: list[dict[str, Any]] | None = None,
) -> SensitivityDecision:
    """Fail closed for low-value proactive outreach, while leaving chat untouched."""
    categories: list[str] = []
    if category in SENSITIVE_CATEGORY_SLUGS:
        categories.append(str(category))
    if explicit_sensitive or any(
        node.get(GF.INFERRED_SENSITIVE) is True for node in (graph_nodes or [])
    ):
        return _decision("sensitive", categories or ["private"], "structured_signal", "explicit")
    if categories:
        return _decision("sensitive", categories, "canonical_category", "category")
    subject = (text or "").strip()
    if not subject:
        return _decision("unknown", ["ambiguous_private"], "empty_subject", "missing context")
    try:
        result = await asyncio.wait_for(
            get_model_provider().cheap(
                f"Subject and proposed copy:\n<subject>{subject[:4000]}</subject>",
                system=_SYSTEM_PROMPT,
                response_model=_Judgment,
                temperature=0.0,
            ),
            timeout=_CLASSIFIER_TIMEOUT_S,
        )
    except Exception as exc:
        logger.error("thread sensitivity classifier unavailable; suppressing proactive outreach", {
            "error_type": type(exc).__name__,
        })
        return _decision(
            "unknown",
            ["classifier_unavailable"],
            "classifier_unavailable",
            "fail_closed",
        )
    judgment = cast(_Judgment, result)
    model_categories = [str(item).strip() for item in judgment.categories if str(item).strip()]
    if judgment.sensitive or judgment.plausibly_sensitive:
        return _decision(
            "sensitive",
            model_categories or ["ambiguous_private"],
            "semantic_classifier",
            judgment.reason or "semantic sensitivity",
        )
    return _decision("clear", [], "semantic_classifier", judgment.reason or "ordinary subject")


async def revalidate_thread_proposal(
    proposal: NotificationProposal,
) -> SensitivityDecision:
    """Re-read the thread and generated copy immediately before channel delivery."""
    from . import thread_store

    thread_id = str((proposal.data or {}).get("thread_id") or "")
    thread = await thread_store.get_thread(proposal.user_id, thread_id) if thread_id else None
    if thread is None:
        return _decision(
            "unknown", ["missing_thread"], "delivery_revalidation", "missing context"
        )
    generated_replies = str((proposal.data or {}).get("suggested_replies") or "")
    subject_and_copy = "\n".join((
        thread.trigger_text,
        thread.known_summary,
        proposal.title,
        proposal.body,
        generated_replies,
    ))
    entity_keys = [str(item) for item in thread.sensitivity.get("entity_keys") or []]
    try:
        graph_nodes = await read_graph_sensitivity_nodes(proposal.user_id, entity_keys)
    except Exception as exc:
        logger.error("thread sensitivity graph revalidation failed; suppressing outreach", {
            "user_id": proposal.user_id,
            "thread_id": thread.thread_id,
            "error_type": type(exc).__name__,
        })
        decision = _decision(
            "unknown", ["graph_unavailable"], "delivery_revalidation", "fail_closed"
        )
        decision_doc = decision.to_dict()
        if entity_keys:
            decision_doc["entity_keys"] = entity_keys
        await thread_store.update_sensitivity(
            proposal.user_id,
            thread.thread_id,
            decision_doc,
            suppress=False,
        )
        return decision
    decision = await classify_proactive_subject(
        subject_and_copy,
        category=thread.category,
        explicit_sensitive=thread.sensitivity.get("status") == "sensitive",
        graph_nodes=graph_nodes,
    )
    decision_doc = decision.to_dict()
    if entity_keys:
        decision_doc["entity_keys"] = entity_keys
    await thread_store.update_sensitivity(
        proposal.user_id,
        thread.thread_id,
        decision_doc,
        suppress=decision.status == "sensitive",
    )
    return decision
