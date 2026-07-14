"""
Coverage for the Buddy Keyboard vocab hints + GET /keyboard/vocab handler.

Pins the contracts for the read-only, consent-gated known-word set the on-device keyboard
caches:
  - subjects + storyline entities are tokenized into deduped word tokens;
  - revoked/empty consent (empty profile) yields an empty list, never an error;
  - an aura read failure degrades to empty, never raises;
  - auth is required (no uid -> 401);
  - the handler response always carries the "tokens" key the Android client reads.
"""

from __future__ import annotations

from src.handlers import keyboard
from src.services.keyboard import vocab


class _Req:
    """Minimal stand-in for a FastAPI Request (auth is monkeypatched; body is unused)."""

    async def json(self):
        return {}


# --- vocab builder unit tests ----------------------------------------------------


async def test_builds_tokens_from_subjects_and_storyline_entities(monkeypatch):
    async def _fetch(uid):
        return {"interests": {"sports": {}}}, []

    monkeypatch.setattr(vocab, "fetch_cached_aura_data", _fetch)
    monkeypatch.setattr(vocab, "top_interest_subjects", lambda profile, **k: ["KCR", "XUV 3XO"])
    monkeypatch.setattr(
        vocab,
        "ranked_storylines",
        lambda profile, **k: [{"entities": ["Annapurna Labs", "AWS"]}],
    )

    hints = await vocab.build_vocab_hints("uid1")

    # "XUV 3XO" -> "XUV" (the digit token is dropped); names from entities are split into words.
    assert "KCR" in hints.tokens
    assert "XUV" in hints.tokens
    assert "Annapurna" in hints.tokens and "Labs" in hints.tokens
    assert "AWS" in hints.tokens
    assert "3XO" not in hints.tokens  # has a digit -> not a word token


async def test_tokens_are_deduplicated_caseInsensitively(monkeypatch):
    async def _fetch(uid):
        return {"interests": {}}, []

    monkeypatch.setattr(vocab, "fetch_cached_aura_data", _fetch)
    monkeypatch.setattr(vocab, "top_interest_subjects", lambda profile, **k: ["Tesla"])
    monkeypatch.setattr(vocab, "ranked_storylines", lambda profile, **k: [{"entities": ["tesla"]}])

    hints = await vocab.build_vocab_hints("uid1")

    assert [t.lower() for t in hints.tokens].count("tesla") == 1


async def test_revoked_or_empty_profile_yields_empty(monkeypatch):
    # fetch_cached_aura_data returns {} when consent is revoked or there is no profile.
    async def _fetch(uid):
        return {}, []

    hit = {"subjects": False}

    def _subjects(profile, **k):
        hit["subjects"] = True
        return ["should not be used"]

    monkeypatch.setattr(vocab, "fetch_cached_aura_data", _fetch)
    monkeypatch.setattr(vocab, "top_interest_subjects", _subjects)

    hints = await vocab.build_vocab_hints("uid1")

    assert hints.tokens == []
    assert hit["subjects"] is False  # empty profile short-circuits before reading


async def test_read_failure_degrades_to_empty(monkeypatch):
    async def _fetch(uid):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(vocab, "fetch_cached_aura_data", _fetch)

    hints = await vocab.build_vocab_hints("uid1")

    assert hints.tokens == []  # never raises into the request


async def test_tokens_are_capped(monkeypatch):
    async def _fetch(uid):
        return {"interests": {}}, []

    # Distinct, purely-alphabetic tokens (digits would be stripped and collapse to duplicates).
    many = [a + b for a in "abcdefgh" for b in "abcdefghijklmnop"]  # 128 distinct tokens
    assert len(many) > vocab.VOCAB_TOKENS_MAX
    monkeypatch.setattr(vocab, "fetch_cached_aura_data", _fetch)
    monkeypatch.setattr(vocab, "top_interest_subjects", lambda profile, **k: many)
    monkeypatch.setattr(vocab, "ranked_storylines", lambda profile, **k: [])

    hints = await vocab.build_vocab_hints("uid1")

    assert len(hints.tokens) == vocab.VOCAB_TOKENS_MAX


# --- handler tests ---------------------------------------------------------------


async def test_handler_requires_auth(monkeypatch):
    monkeypatch.setattr(keyboard, "resolve_keyboard_uid", lambda r: None)
    resp = await keyboard.handle_keyboard_vocab(_Req())
    assert resp.status_code == 401


async def test_handler_returns_tokens_key(monkeypatch):
    monkeypatch.setattr(keyboard, "resolve_keyboard_uid", lambda r: "uid1")

    async def _build(uid):
        return vocab.VocabHints(tokens=["KCR", "Verstappen"])

    monkeypatch.setattr(keyboard, "build_vocab_hints", _build)

    resp = await keyboard.handle_keyboard_vocab(_Req())
    assert resp.status_code == 200
    import json

    body = json.loads(resp.body)
    assert body["tokens"] == ["KCR", "Verstappen"]  # the contract the Android client reads
