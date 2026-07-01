"""Buddy Keyboard backend package.

The brain behind POST /keyboard/draft. Memory-aware drafting (reply/continue/rewrite)
in the user's voice, plus pure-utility actions (grammar/translate/tone). A memory
CONSUMER, never a producer: nothing the user types is persisted.
"""
