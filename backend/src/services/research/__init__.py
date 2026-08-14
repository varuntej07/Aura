"""Background research acquisition primitives.

Phase one is deliberately standalone: nothing in this package is registered as a
route, tool, Cloud Task target, or scheduler hook. `acquire.acquire_research_sources`
is reachable only by an explicit import.
"""
