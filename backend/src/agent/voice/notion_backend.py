"""Shared voice-worker client for the backend's /notion/* and /research routes.

notion_capture.py (Phase 1 capture) and research_dispatch.py (Phase 2
research) each grew their own copy of the same three things: the authed HTTP
call, the destination resolve request, and the resolve->bind/ask/propose
decision tree. The copies had already drifted (three different timeout
constants for the same two routes, and the run-identity divergence that
shipped a real bug), so all three live here exactly once. Spoken copy stays
with each tool: this module returns typed outcomes, never sentences, except
through the DestinationCopy the caller supplies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ...config.settings import settings


class ReauthorizationRequired(Exception):
    """Backend answered 409: the user's Notion connection needs a reconnect."""


class BackendCallFailed(Exception):
    """Backend answered non-200/409; the message carries path and status."""


def voice_backend_headers(firebase_id_token: str, session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {firebase_id_token}",
        "X-Aura-Voice-Session": session_id,
    }


async def request_backend(
    method: str,
    path: str,
    *,
    firebase_id_token: str,
    session_id: str,
    json_body: dict | None = None,
    timeout_s: float,
) -> httpx.Response:
    """One authed request; the caller interprets the status code."""
    url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(
            method,
            url,
            json=json_body,
            headers=voice_backend_headers(firebase_id_token, session_id),
        )


async def post_backend(
    path: str,
    body: dict,
    *,
    firebase_id_token: str,
    session_id: str,
    timeout_s: float,
) -> dict:
    """POST expecting 200 JSON; 409 raises ReauthorizationRequired, anything
    else non-200 raises BackendCallFailed."""
    response = await request_backend(
        "POST",
        path,
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        json_body=body,
        timeout_s=timeout_s,
    )
    if response.status_code == 409:
        raise ReauthorizationRequired()
    if response.status_code != 200:
        raise BackendCallFailed(f"{path} -> {response.status_code}")
    return response.json()


async def resolve_spoken_destination(
    *,
    destination: str,
    firebase_id_token: str,
    session_id: str,
    timeout_s: float,
) -> dict:
    return await post_backend(
        "/notion/resolve",
        {"spoken_destination": destination},
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        timeout_s=timeout_s,
    )


async def create_database_backend(
    *,
    name: str,
    firebase_id_token: str,
    session_id: str,
    timeout_s: float,
) -> tuple[str, str]:
    """Create the voice-confirmed database; returns (data_source_id, name)."""
    created = await post_backend(
        "/notion/create-database",
        {"name": name},
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        timeout_s=timeout_s,
    )
    return (
        str(created.get("data_source_id") or ""),
        str(created.get("database_name") or name),
    )


@dataclass(frozen=True, slots=True)
class DestinationCopy:
    """The two spoken templates that differ between the tools."""

    ask_format: str  # receives {titles}
    propose_format: str  # receives {name}


@dataclass(frozen=True, slots=True)
class DestinationDecision:
    """Either a bound destination or the question the model should relay.

    bound: data_source_id is non-empty and the write may proceed.
    question: set for ask/propose outcomes; candidates or
    proposed_create_name carry the machine half of the same question.
    """

    data_source_id: str = ""
    database_name: str = ""
    question: str | None = None
    candidates: list[tuple[str, str]] = field(default_factory=list)
    proposed_create_name: str | None = None

    @property
    def bound(self) -> bool:
        return bool(self.data_source_id)


def decide_destination(
    resolved: dict | None,
    *,
    destination: str,
    confirmed_data_source_id: str,
    confirmed_database_name: str,
    copy: DestinationCopy,
) -> DestinationDecision:
    """The shared bind/ask/propose decision, pure and copy-parameterized.

    Only the user's words (or their confirmed choice) ever decide the
    destination; nothing screen- or web-derived is a candidate input.
    """
    if confirmed_data_source_id:
        return DestinationDecision(
            data_source_id=confirmed_data_source_id,
            database_name=confirmed_database_name,
        )
    if resolved is None:
        return DestinationDecision()

    outcome = str(resolved.get("outcome") or "")
    if outcome == "bind":
        return DestinationDecision(
            data_source_id=str(resolved.get("data_source_id") or ""),
            database_name=str(resolved.get("title") or destination),
        )
    if outcome == "ask":
        candidates = [
            (str(item.get("data_source_id") or ""), str(item.get("title") or ""))
            for item in resolved.get("candidates", [])
            if item.get("data_source_id")
        ]
        titles = " or ".join(title for _, title in candidates[:2])
        return DestinationDecision(
            question=copy.ask_format.format(titles=titles),
            candidates=candidates,
        )
    # propose_create / no_databases / anything unrecognized: propose creating,
    # named strictly from the user's own words.
    name = " ".join(destination.split())[:80]
    return DestinationDecision(
        question=copy.propose_format.format(name=name),
        proposed_create_name=name,
    )
