"""
Single source of truth for the one message shown to the user when the entire
text-chat fallback chain (Sonnet -> Haiku -> Gemini Flash -> GPT-4.1-mini) is
exhausted.

Every layer that can terminate the chat stream (claude_client.py,
gemini_chat_fallback.py, openai_chat_fallback.py, chat.py) imports this same
constant. Raw provider exception text (API error bodies, billing messages,
stack traces) must never reach the client -- log it via logger.exception, but
the only text a user is ever shown is this line.
"""

from __future__ import annotations

CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE = (
    "Something glitched on my end there. Mind sending that again?"
)
