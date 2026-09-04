"""Notion capture services: tool-free extraction, destination resolution,
schema snapshots, and the deterministic page writer.

The firebreak (Aura-Desktop/future-features.txt section 3) is enforced by
construction across these modules: extract.py's model calls hold no tools,
resolve.py sees only the user's spoken words, and write.py contains no model.
"""
