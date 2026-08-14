"""bridge_handover.py — worker side of the Realtime -> LiveKit voice handover.

The desktop opens an instant OpenAI Realtime leg on double-tap while this LiveKit worker
cold-starts. When the worker is ready it enters HOLD (no greeting) and announces itself
with ``hold_ready``. The desktop then either:

- aborts (LiveKit won the race before Realtime spoke): sends ``handover_skip`` and this
  agent greets normally, as if it were a fresh session, or
- bridges: finalizes the Realtime turn and sends ``handover_begin`` with the ordered
  transcript (and an optional pending action intent). This agent seeds that transcript
  into its ChatContext and, if there is a pending intent, generates a continuation.

Every inbound control message carries a ``handover_id`` and is acknowledged with
``handover_applied`` so the desktop never swaps audio before this agent has actually taken
over. HOLD is kept alive by ``bridge_heartbeat`` messages, not a fixed timer: if the
desktop goes silent (crash) we assume the bridge is dead and greet normally so the user is
never left with a mute agent.

Wire protocol (JSON over the LiveKit data channel):
  worker -> desktop : {"type":"hold_ready"}
                      {"type":"handover_applied","handover_id":"<id>"}
  desktop -> worker : {"type":"handover_begin","handover_id":"<id>",
                       "turns":[{"role":"user|assistant","text":"..."}],
                       "pending_intent":"<optional>"}
                      {"type":"handover_skip","handover_id":"<id>"}
                      {"type":"bridge_heartbeat"}
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...prompts import DESKTOP_BRIDGE_CONTINUATION_INSTRUCTIONS

if TYPE_CHECKING:
    from livekit.agents import AgentSession
    from livekit.rtc import Room

    from ..buddy_agent import BuddyAgent

# Inbound (desktop -> worker)
HANDOVER_BEGIN_TYPE = "handover_begin"
HANDOVER_SKIP_TYPE = "handover_skip"
BRIDGE_HEARTBEAT_TYPE = "bridge_heartbeat"
# Outbound (worker -> desktop)
HOLD_READY_TYPE = "hold_ready"
HANDOVER_APPLIED_TYPE = "handover_applied"

BRIDGE_CONTROL_TYPES = frozenset(
    {HANDOVER_BEGIN_TYPE, HANDOVER_SKIP_TYPE, BRIDGE_HEARTBEAT_TYPE}
)

# Seed bounds: the transcript is untrusted client content, so cap it hard.
_MAX_TURNS = 40
_MAX_TURN_CHARS = 4000
# HOLD is released if no heartbeat lands within this window. Generous enough to cover the
# gap between hold_ready and the desktop's first heartbeat, tight enough that a dead
# desktop does not leave the agent mute for long.
_HEARTBEAT_TIMEOUT_S = 8.0



class BridgeHandoverCoordinator:
    """Owns the worker's HOLD state and the acknowledged handover to the LiveKit cascade."""

    def __init__(
        self,
        *,
        session: "AgentSession",
        buddy: "BuddyAgent",
        room: "Room",
        session_id: str,
        user_id: str,
    ) -> None:
        self._session = session
        self._buddy = buddy
        self._room = room
        self._session_id = session_id
        self._user_id = user_id
        self._applied_ids: set[str] = set()
        self._last_heartbeat = time.monotonic()
        self._done = asyncio.Event()
        self._monitor_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Announce HOLD (``hold_ready``) and begin watching for lost heartbeats.

        Called once, right after ``session.start`` and after the data listener is
        installed, so the desktop's first control packet cannot be dropped.
        """
        self._last_heartbeat = time.monotonic()
        await self._emit({"type": HOLD_READY_TYPE})
        logger.info(
            "bridge: hold_ready sent",
            {"session_id": self._session_id},
        )
        self._monitor_task = asyncio.create_task(
            self._monitor(), name=f"bridge-hold-{self._session_id[:8]}"
        )

    def handle(self, msg: dict) -> None:
        """Dispatch a bridge control message. Safe to call from the sync data callback.

        Heartbeats are handled inline; begin/skip schedule async work (context mutation,
        generation, and the acknowledgment publish).
        """
        mtype = msg.get("type")
        if mtype == BRIDGE_HEARTBEAT_TYPE:
            self._last_heartbeat = time.monotonic()
        elif mtype == HANDOVER_BEGIN_TYPE:
            asyncio.create_task(
                self._apply_begin(msg), name=f"bridge-begin-{self._session_id[:8]}"
            )
        elif mtype == HANDOVER_SKIP_TYPE:
            asyncio.create_task(
                self._apply_skip(msg), name=f"bridge-skip-{self._session_id[:8]}"
            )

    async def aclose(self) -> None:
        """Stop the heartbeat monitor (session teardown)."""
        self._done.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()

    async def _apply_begin(self, msg: dict) -> None:
        handover_id = str(msg.get("handover_id") or "")
        if not handover_id:
            return
        if handover_id in self._applied_ids:
            # Idempotent: a retried begin (same id) just re-acks.
            await self._emit_applied(handover_id)
            return

        logger.info(
            "bridge: handover_begin received",
            {"session_id": self._session_id, "handover_id": handover_id},
        )
        seeded = self._seed_context(msg.get("turns") or [])
        try:
            await self._buddy.update_chat_ctx(seeded)
        except Exception as exc:
            # If seeding fails we still take over (greet-fresh) rather than leave the user
            # hanging; better a lost transcript than a dead session.
            logger.warn(
                "bridge: seed context failed, taking over without history",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

        self._applied_ids.add(handover_id)
        self._done.set()

        # Ack FIRST so the desktop unmutes LiveKit audio and swaps the mic, THEN generate
        # any continuation - otherwise the first words of the continuation play into a
        # still-muted sink and get clipped.
        await self._emit_applied(handover_id)

        pending_intent = str(msg.get("pending_intent") or "").strip()
        if pending_intent:
            try:
                capture = await self._buddy.handle_bridged_screen_capture(
                    pending_intent,
                    request_id=handover_id,
                )
                if capture is not None:
                    command, result = capture
                    await self._session.say(
                        result.spoken_confirmation,
                        allow_interruptions=True,
                    )
                    if command.command_only:
                        return
                    pending_intent = command.remainder
                await self._session.generate_reply(
                    instructions=DESKTOP_BRIDGE_CONTINUATION_INSTRUCTIONS.format(
                        intent=pending_intent[:_MAX_TURN_CHARS]
                    )
                )
            except Exception as exc:
                logger.warn(
                    "bridge: continuation generation failed",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )

    async def _apply_skip(self, msg: dict) -> None:
        handover_id = str(msg.get("handover_id") or "")
        self._done.set()
        if handover_id and handover_id not in self._applied_ids:
            self._applied_ids.add(handover_id)
            # Acknowledge ownership before generating the greeting. The desktop
            # keeps LiveKit audio muted until this packet arrives, so greeting
            # first would clip the beginning of the worker's first message.
            await self._emit_applied(handover_id)
            # Abort path: Realtime never spoke, so greet as a fresh session.
            try:
                await self._buddy.greet()
            except Exception as exc:
                logger.warn(
                    "bridge: skip greeting failed",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
        elif handover_id:
            # Idempotent retry: acknowledge again without greeting twice.
            await self._emit_applied(handover_id)

    def _seed_context(self, turns: list) -> lk_llm.ChatContext:
        """Build a new ChatContext = current history + validated, bounded prior turns.

        Turns are untrusted client content: role-validated, per-turn char-capped, and
        count-capped. They are added as plain conversational messages, never as
        instructions.
        """
        new_ctx = self._buddy.chat_ctx.copy()
        added = 0
        for turn in turns:
            if added >= _MAX_TURNS:
                break
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            text = str(turn.get("text") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            new_ctx.add_message(role=role, content=[text[:_MAX_TURN_CHARS]])
            added += 1
        logger.info(
            "bridge: seeded handover transcript",
            {"session_id": self._session_id, "turns_seeded": added},
        )
        return new_ctx

    async def _monitor(self) -> None:
        while not self._done.is_set():
            await asyncio.sleep(1.0)
            if self._done.is_set():
                return
            if time.monotonic() - self._last_heartbeat > _HEARTBEAT_TIMEOUT_S:
                logger.warn(
                    "bridge: heartbeat lost, greeting normally",
                    {"session_id": self._session_id, "user_id": self._user_id},
                )
                self._done.set()
                try:
                    await self._buddy.greet()
                except Exception as exc:
                    logger.warn(
                        "bridge: fallback greeting failed",
                        {
                            "session_id": self._session_id,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
                return

    async def _emit_applied(self, handover_id: str) -> None:
        await self._emit({"type": HANDOVER_APPLIED_TYPE, "handover_id": handover_id})
        logger.info(
            "bridge: handover_applied sent",
            {"session_id": self._session_id, "handover_id": handover_id},
        )

    async def _emit(self, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            await self._room.local_participant.publish_data(data, reliable=True)
        except Exception as exc:
            logger.warn(
                "bridge: control publish failed",
                {
                    "session_id": self._session_id,
                    "type": payload.get("type"),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
