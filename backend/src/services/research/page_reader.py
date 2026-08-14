"""The replaceable page-body acquisition seam.

Deliberately smaller than a scraping framework: one method, one bounded request, one
stateful result. Everything Aura cares about (search policy, claim extraction, source
classification) stays inside Aura's own stages, so swapping the provider never moves
product logic.

Matches the one existing runtime-checkable seam in the backend, `reactive/agent.py`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import PageReadRequest, PageReadResult


@runtime_checkable
class PageReader(Protocol):
    """Reads one already-validated public URL as markdown plus metadata.

    Implementations MUST NOT raise on network, timeout, or provider failure: those
    degrade to a PageReadResult carrying the matching PageReadState, so one bad page
    never kills a wave. A missing credential is the one exception and SHOULD raise,
    because it is a deploy misconfiguration a developer must see.
    """

    async def read(self, request: PageReadRequest) -> PageReadResult: ...
