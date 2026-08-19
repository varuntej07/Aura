"""Job-description transfer: one revisioned overlay, one bounded byte stream.

A byte stream, not an upload. The JD never touches Firestore, GCS or a REST
endpoint; the decoded text lands in ``session.userdata`` and dies with the
session. Nothing here persists anything.

Two waits, both bounded, in this order:

1. The overlay request goes out on the reliable ``client_events`` topic and is
   acknowledged. This follows ``voice/artifact_delivery.py`` exactly, including
   its lesson: publishing a packet is not evidence the user can see a paste box,
   so the waiter is armed BEFORE publishing and a missing ack means "no overlay",
   never "probably fine".
2. The material itself arrives on the ``interview_material`` byte stream, matched
   on (interview_id, revision). A human has to find the posting and paste it, so
   this wait is long, but still bounded, so a user who wandered off cannot pin
   the intake open.

The payload is UNTRUSTED: text pasted out of someone elses web page. Every path
here is fail-soft. An exception escaping into the session drops the whole turn,
so nothing in this module may raise.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from ....lib.logger import logger
from .contracts import (
    ATTR_INTERVIEW_ID,
    ATTR_MATERIAL_TYPE,
    ATTR_REVISION,
    ATTR_SCHEMA_VERSION,
    MATERIAL_ASSEMBLY_TIMEOUT_S,
    MATERIAL_REQUEST_TYPE,
    MATERIAL_SCHEMA_VERSION,
    MAX_MATERIAL_BYTES,
    OVERLAY_ACK_TIMEOUT_S,
    SUPPORTED_MATERIAL_SCHEMA_VERSIONS,
)

PUBLISH_TIMEOUT_S = 2.0


def _key(interview_id: str, revision: int) -> str:
    return f"{interview_id}:{revision}"


class InterviewMaterialStore:
    """Receives interview materials for one voice session, and nothing else."""

    def __init__(
        self, *, session_id: str, user_id: str, client_events_topic: str
    ) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._client_events_topic = client_events_topic
        self._assembly_tasks: set[asyncio.Task] = set()
        # Armed by request_material_overlay BEFORE the request is published, so a
        # paste that somehow beats our own await still resolves the right waiter.
        self._expected_key = ""
        self._material = ""
        self._arrived = asyncio.Event()
        self._overlay_shown = asyncio.Event()
        self._received_count = 0
        # Set the instant one stream wins the arming, in the same synchronous
        # block that publishes its text. See _assemble.
        self._claimed = False
        # The ownership epoch this arming belongs to. Read back by the intake task
        # after its await, so text that arrived for an interview the session has
        # since left is discarded rather than committed. Kept here rather than on
        # the wire because the desktop already correlates on (interview_id,
        # revision), and widening that payload would move a cross-repo contract.
        self._armed_epoch = -1

    @property
    def received_count(self) -> int:
        return self._received_count

    @property
    def armed_epoch(self) -> int:
        return self._armed_epoch

    def arm(self, *, interview_id: str, revision: int, ownership_epoch: int) -> None:
        """Accept exactly one (interview_id, revision) from now on.

        Arming replaces any previous expectation: a new revision means the old
        paste box is gone, and text still in flight for it is stale by definition.
        """
        self._expected_key = _key(interview_id, revision)
        self._material = ""
        self._claimed = False
        self._armed_epoch = ownership_epoch
        self._arrived = asyncio.Event()
        self._overlay_shown = asyncio.Event()

    def disarm(self) -> None:
        """Stop accepting material. Anything in flight is dropped on arrival."""
        self._expected_key = ""

    async def aclose(self) -> None:
        """Drop the arming and stop every in-flight assembly. Never raises.

        Registered as a session shutdown callback. Without it a stalled reader
        keeps its task, and the bytes it had already accumulated, alive past the
        session that asked for them.
        """
        self.disarm()
        tasks = list(self._assembly_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._assembly_tasks.clear()

    async def wait_for_overlay(self, timeout_s: float = OVERLAY_ACK_TIMEOUT_S) -> bool:
        """True when the client confirmed the paste overlay is actually on screen."""
        return await self._wait(self._overlay_shown, timeout_s, "overlay_ack")

    async def wait_for_material(self, timeout_s: float) -> str:
        """The pasted text, or empty string if none arrived inside the bound."""
        if not await self._wait(self._arrived, timeout_s, "material_arrival"):
            return ""
        return self._material

    async def _wait(self, event: asyncio.Event, timeout_s: float, what: str) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "InterviewMaterial: wait failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "waiting_for": what,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    def handle_overlay_ack(
        self, msg: dict, participant_identity: str, topic: str
    ) -> None:
        """Resolve the overlay waiter for one acknowledged request revision."""
        if participant_identity != self._user_id or topic != self._client_events_topic:
            logger.warn(
                "InterviewMaterial: overlay ack rejected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "participant": participant_identity,
                    "topic": topic,
                },
            )
            return
        interview_id = msg.get("interview_id")
        revision = msg.get("revision")
        if not isinstance(interview_id, str) or not interview_id:
            return
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return
        if _key(interview_id, revision) != self._expected_key:
            return
        self._overlay_shown.set()

    def handle_stream(self, reader, participant_identity: str) -> None:
        """Sync callback for ``room.register_byte_stream_handler``; assembles async."""
        task = asyncio.create_task(
            self._assemble_bounded(reader, participant_identity),
            name=f"voice-interview-material-{self._session_id[:8]}",
        )
        self._assembly_tasks.add(task)
        task.add_done_callback(self._assembly_tasks.discard)

    async def _assemble_bounded(self, reader, participant_identity: str) -> None:
        """Assemble one stream under a hard bound. Never raises."""
        try:
            await asyncio.wait_for(
                self._assemble(reader, participant_identity),
                timeout=MATERIAL_ASSEMBLY_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warn(
                "InterviewMaterial: rejected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "reason": "material_assembly_timeout",
                    "timeout_s": MATERIAL_ASSEMBLY_TIMEOUT_S,
                },
            )
        except asyncio.CancelledError:
            raise

    async def _assemble(self, reader, participant_identity: str) -> None:
        try:
            attributes = dict(getattr(reader.info, "attributes", None) or {})
            reason = self._reject_reason(attributes, participant_identity)
            if reason:
                self._reject(reason, 0, attributes)
                return

            chunks = bytearray()
            async for chunk in reader:
                chunks.extend(chunk)
                if len(chunks) > MAX_MATERIAL_BYTES:
                    # Dropped without being decoded. Oversize is a bug or abuse,
                    # not an unlucky long posting.
                    self._reject(
                        "material_size_limit_exceeded", len(chunks), attributes
                    )
                    return
            if not chunks:
                self._reject("empty_material_stream", 0, attributes)
                return
            try:
                text = bytes(chunks).decode("utf-8")
            except UnicodeDecodeError:
                self._reject("material_not_utf8", len(chunks), attributes)
                return
            if not text.strip():
                self._reject("blank_material", len(chunks), attributes)
                return

            # Re-checked after assembly: a newer revision may have been armed
            # while these bytes were in flight, which makes them stale on arrival.
            #
            # This block through _arrived.set() contains NO await, so under the
            # event loop it is atomic: the first stream to reach it claims the
            # arming, and a second one that was assembling concurrently sees
            # _claimed and is rejected instead of overwriting the text the intake
            # task may already have read.
            if self._attribute_key(attributes) != self._expected_key:
                self._reject("revision_superseded_in_flight", len(chunks), attributes)
                return
            if self._claimed:
                self._reject("material_already_received", len(chunks), attributes)
                return

            self._claimed = True
            self._material = text
            self._received_count += 1
            self._arrived.set()
            logger.info(
                "InterviewMaterial: received",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "material_type": attributes.get(ATTR_MATERIAL_TYPE, ""),
                    "revision": attributes.get(ATTR_REVISION, ""),
                    "chars": len(text),
                    "raw_bytes": len(chunks),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "InterviewMaterial: assembly failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error_type": type(exc).__name__,
                },
            )

    @staticmethod
    def _attribute_key(attributes: dict) -> str:
        try:
            revision = int(attributes.get(ATTR_REVISION, ""))
        except (TypeError, ValueError):
            return ""
        return _key(attributes.get(ATTR_INTERVIEW_ID, ""), revision)

    def _reject_reason(self, attributes: dict, participant_identity: str) -> str:
        """Why this stream must not be accepted, or empty string if it may be."""
        if participant_identity != self._user_id:
            return "material_participant_rejected"
        if not self._expected_key:
            return "no_material_requested"
        if self._claimed:
            # Cheap early out for the common duplicate: a stream that arrives
            # after one has already won is dropped without being read at all. The
            # authoritative check is the atomic one in _assemble, which also
            # covers a stream that was mid-flight when the winner landed.
            return "material_already_received"
        raw_version = attributes.get(ATTR_SCHEMA_VERSION, str(MATERIAL_SCHEMA_VERSION))
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            return "material_schema_version_invalid"
        if version not in SUPPORTED_MATERIAL_SCHEMA_VERSIONS:
            return "material_schema_version_unsupported"
        if attributes.get(ATTR_MATERIAL_TYPE, "") != "job_description":
            return "material_type_unsupported"
        key = self._attribute_key(attributes)
        if not key:
            return "material_revision_invalid"
        if key != self._expected_key:
            # The most important check here. A paste answering an earlier request
            # would otherwise be silently accepted as the answer to this one,
            # which is exactly the class of bug that looks like nothing at all.
            return "material_revision_mismatch"
        return ""

    def _reject(self, reason: str, size: int, attributes: dict) -> None:
        logger.warn(
            "InterviewMaterial: rejected",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "reason": reason,
                "raw_bytes": size,
                "interview_id": attributes.get(ATTR_INTERVIEW_ID, ""),
                "revision": attributes.get(ATTR_REVISION, ""),
                "material_type": attributes.get(ATTR_MATERIAL_TYPE, ""),
            },
        )


async def request_material_overlay(
    *,
    store: InterviewMaterialStore,
    room,
    interview_id: str,
    revision: int,
    ownership_epoch: int,
    material_type: str = "job_description",
) -> bool:
    """Ask the desktop to show one paste overlay. True when it confirms drawing it.

    Never raises: a failed publish is a False, not a dropped turn.
    """
    store.arm(
        interview_id=interview_id,
        revision=revision,
        ownership_epoch=ownership_epoch,
    )
    event = {
        "type": MATERIAL_REQUEST_TYPE,
        "payload": {
            "schema_version": MATERIAL_SCHEMA_VERSION,
            "interview_id": interview_id,
            "revision": revision,
            "material_type": material_type,
        },
    }
    data = json.dumps(event, ensure_ascii=False).encode("utf-8")

    async def _publish() -> bool:
        try:
            await asyncio.wait_for(
                room.local_participant.publish_data(data, reliable=True),
                timeout=PUBLISH_TIMEOUT_S,
            )
            return True
        except Exception as exc:
            logger.warn(
                "InterviewMaterial: overlay request publish failed",
                {"interview_id": interview_id, "error_type": type(exc).__name__},
            )
            return False

    if not await _publish():
        return False
    if await store.wait_for_overlay():
        return True
    # One idempotent resend at the same id and revision, so an overlay that did
    # mount is deduped by the client rather than drawn twice.
    if await _publish():
        return await store.wait_for_overlay()
    return False
