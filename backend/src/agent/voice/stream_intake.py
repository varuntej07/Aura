"""The parts every byte-stream store on this worker does identically.

Three stores read a LiveKit byte stream: screen frames, structured screen
context, and interview materials. Their validation is genuinely different, so
there is no useful common base class. What IS identical, and what was copied
three times, is the plumbing around it: spawning the assembly task and holding a
strong reference to it, refusing a stream from anyone but the session owner,
reading it under a byte cap, and remembering which turn ids have been consumed.

Deliberately composition, not inheritance. A base class would need a hook for
each of the three stores' different decode, freshness and listener semantics,
which is how you end up with an abstraction that is harder to follow than the
duplication it replaced.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Turn ids a finalized turn already attached. Bounded so a long session cannot
# grow it, and large enough that no realistic reordering re-attaches an old one.
DEFAULT_CONSUMED_RING_SIZE = 16


class AssemblyTasks:
    """Strong references to in-flight stream assembly tasks.

    Without the reference the event loop can garbage-collect a running task
    mid-assembly, which loses the payload with no error anywhere.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Any, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def drain(self) -> None:
        """Cancel every task and wait for it, so none outlives the session."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    def __len__(self) -> int:
        return len(self._tasks)


class ConsumedIdRing:
    """Bounded memory of turn ids a turn has already consumed."""

    def __init__(self, size: int = DEFAULT_CONSUMED_RING_SIZE) -> None:
        self._size = size
        self._ids: list[str] = []

    def __contains__(self, turn_context_id: object) -> bool:
        return turn_context_id in self._ids

    def remember(self, turn_context_id: str) -> None:
        if not turn_context_id or turn_context_id in self._ids:
            return
        self._ids.append(turn_context_id)
        overflow = len(self._ids) - self._size
        if overflow > 0:
            del self._ids[0:overflow]

    def clear(self) -> None:
        self._ids.clear()


async def read_stream_bounded(reader: Any, max_bytes: int) -> bytearray | None:
    """Accumulate a byte stream, or None once it passes ``max_bytes``.

    None means the cap was exceeded and the payload must be dropped; an empty
    result means the stream carried nothing. The two are different faults and
    every caller logs them differently, so they stay distinguishable here.
    """
    chunks = bytearray()
    async for chunk in reader:
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            return None
    return chunks
