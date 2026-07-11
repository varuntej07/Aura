"""Outbound Drafts persistence: the durable, per-user store behind the
dashboard's Drafts feed. The voice worker creates a doc for every draft it
mints, refines (voice or REST chips) overwrite the text in place, and a
7-day Firestore TTL expires what the user never deletes.

See ``fields.py`` for the document contract and ``store.py`` for the
writer/reader shared by the voice tool and the REST handlers.
"""
