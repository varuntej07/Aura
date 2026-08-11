"""Delivery acknowledgement for overlay artifacts.

Publishing ``overlay.artifact.ready`` proves a packet left the worker. It does
not prove the desktop drew anything, and "Done, it's on your screen." is a claim
about the user's screen, not about the network. A dropped packet, an overlay
that is not mounted, or a card kind an older build cannot render all produce the
same silent nothing while Buddy insists the card is there.

The client echoes ``artifact.displayed`` carrying the id and revision it
rendered. Both already exist on ``artifact_contract.OverlayArtifact``, so
dedupe and an idempotent resend come for free: the same id twice is one card,
and the client drops the repeat.

**Old clients must not be called failures.** Capable builds advertise
``displayed-v1`` in participant metadata and must acknowledge from the first
card. Older builds omit it and retain optimistic delivery. A valid late
acknowledgement can still promote an unadvertised session, but capability is no
longer inferred only after the first unverified card.
"""

from __future__ import annotations

import asyncio

from ...lib.logger import logger

ARTIFACT_DISPLAYED_TYPE = "artifact.displayed"

# One round trip over a reliable data channel on a local network is a few tens
# of milliseconds. This is the outer bound before we stop waiting, not a budget
# we expect to spend: the wait ends the moment the ack lands.
DEFAULT_ACK_TIMEOUT_S = 0.9


def _key(artifact_id: str, revision: int) -> str:
    return f"{artifact_id}:{revision}"


class ArtifactDeliveryTracker:
    """Per-session record of which published artifacts the client confirmed drawing."""

    def __init__(
        self,
        *,
        session_id: str,
        user_id: str,
        client_events_topic: str,
        client_ack_capable: bool = False,
        ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
    ) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._client_events_topic = client_events_topic
        self._ack_timeout_s = ack_timeout_s
        self._pending: dict[str, asyncio.Event] = {}
        self._ack_required = client_ack_capable
        self._client_proven = False

    @property
    def ack_required(self) -> bool:
        """Whether this client explicitly advertised the displayed-card protocol."""
        return self._ack_required

    @property
    def client_proven(self) -> bool:
        """Whether this client has ever acknowledged a card in this session."""
        return self._client_proven

    def expect(self, artifact_id: str, revision: int) -> str:
        """Arm a waiter BEFORE publishing, so a fast ack cannot arrive first."""
        key = _key(artifact_id, revision)
        self._pending.setdefault(key, asyncio.Event())
        return key

    async def wait(self, key: str) -> bool:
        """True when the client confirmed drawing this exact revision in time."""
        event = self._pending.get(key)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=self._ack_timeout_s)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "VoiceSession: artifact ack wait failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    def release(self, key: str) -> None:
        self._pending.pop(key, None)

    def handle_ack(self, msg: dict, participant_identity: str, topic: str) -> None:
        """Resolve the waiter for one acknowledged artifact revision."""
        if participant_identity != self._user_id or topic != self._client_events_topic:
            logger.warn(
                "VoiceSession: artifact ack rejected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "participant": participant_identity,
                    "topic": topic,
                },
            )
            return
        artifact_id = msg.get("artifact_id")
        revision = msg.get("revision")
        if not isinstance(artifact_id, str) or not artifact_id:
            return
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return
        # Proven on ANY well-formed ack, including one for an artifact we already
        # stopped waiting on: it still tells us this build speaks the protocol.
        self._ack_required = True
        self._client_proven = True
        event = self._pending.get(_key(artifact_id, revision))
        if event is not None:
            event.set()
