"""Per-surface capability digest derived from the validated product catalog.

Appended to the STABLE voice prompt prefix so the model always knows what Aura
is and what Buddy can do on the current surface without a tool round trip. A
live 2026-09 desktop session showed the alternative: with no product identity
in context, the model asked the user what app they were using and answered
capability questions with retrieval meta-language.

Everything here is computed once at import from ``PRODUCT_KNOWLEDGE``, never
from per-session state, so a digest is byte-stable for a given
(surface, knowledge_version). That stability is load-bearing: the digest sits
inside the cached prompt prefix (OpenAI ``prompt_cache_key``, Anthropic
ephemeral cache), and any per-session variation here would churn that cache on
every turn. The full catalog stays out of the prompt on purpose; entry answers
remain reachable only through ``get_aura_product_info``.
"""

from __future__ import annotations

from .catalog import PRODUCT_KNOWLEDGE, ProductEntry

_SURFACE_PHRASES: dict[str, str] = {
    "app": "the Aura phone app",
    "keyboard": "Buddy Keyboard voice on Android",
    "desktop": "Aura Desktop",
}

_PLATFORM_PHRASES: dict[str, str] = {
    "android": "Android",
    "ios": "iOS",
    "windows": "Windows",
    "macos": "macOS",
}


def _entry(entry_id: str) -> ProductEntry | None:
    return next(
        (entry for entry in PRODUCT_KNOWLEDGE.entries if entry.id == entry_id), None
    )


def _release_sentence() -> str:
    """One deterministic sentence naming where Aura is and is not available."""
    available: list[str] = []
    unavailable: list[str] = []
    for client in sorted(
        PRODUCT_KNOWLEDGE.product_release.clients,
        key=lambda client: (client.surface, client.platform),
    ):
        phrase = f"{client.surface} on {_PLATFORM_PHRASES.get(client.platform, client.platform)}"
        if client.availability == "available":
            available.append(phrase)
        else:
            unavailable.append(phrase)
    sentence = (
        f"Aura is in {PRODUCT_KNOWLEDGE.product_release.stage.replace('_', ' ')}; "
        f"available today: {', '.join(available)}."
    )
    if unavailable:
        sentence += f" Not yet available: {', '.join(unavailable)}."
    return sentence


def _render(surface: str) -> str:
    surface_entry = _entry(f"capabilities.{surface}") or _entry("capabilities.all")
    cross_entry = _entry("capabilities.all")
    shared_entry = _entry("background.shared_state")
    lines = [
        "<aura_capability_digest>",
        f"Verified Aura facts, catalog {PRODUCT_KNOWLEDGE.knowledge_version}. "
        + _release_sentence(),
    ]
    if surface_entry is not None:
        lines.append(f"Here on {_SURFACE_PHRASES[surface]}: {surface_entry.summary}")
    cross_parts = [
        entry.summary
        for entry in (cross_entry, shared_entry)
        if entry is not None and entry is not surface_entry
    ]
    if cross_parts:
        lines.append("Across devices: " + " ".join(cross_parts))
    lines.append(
        "These are summaries, not the full picture. For any specific feature, "
        "setting, step, or availability question, get_aura_product_info is the "
        "source of truth."
    )
    lines.append("</aura_capability_digest>")
    return "\n" + "\n".join(lines) + "\n"


# Precomputed at import: byte-stable per (surface, knowledge_version), and a
# malformed catalog fails process startup here rather than mid-session.
VOICE_CAPABILITY_DIGESTS: dict[str, str] = {
    surface: _render(surface) for surface in _SURFACE_PHRASES
}


def voice_capability_digest(surface: str) -> str:
    """Return the stable digest for a voice surface ("app", "keyboard", "desktop")."""
    return VOICE_CAPABILITY_DIGESTS[surface]
