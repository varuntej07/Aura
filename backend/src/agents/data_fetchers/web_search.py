"""Backwards-compatible shim. The primitive moved to web_surf.py.

Internal scheduled agents (sports_agent, technews_agent, signal_engine/content_ingest)
import `web_search(query, uid) -> str` from here. Forward to the new primitive and
return just the synthesized text so callers don't need to change.
"""

from __future__ import annotations

from .web_surf import web_surf as _web_surf


async def web_search(query: str, uid: str) -> str:
    result = await _web_surf(query, uid=uid, recency="any")
    return str(result.get("text", ""))
