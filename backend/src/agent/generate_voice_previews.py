"""One-time script: render the voice-picker preview clip for every catalog voice.

Writes one short MP3 per entry in voice_catalog.CATALOG into assets/voices/ at the
repo root, where the Flutter picker bundles them. Run locally, listen to all of
them, then commit. The app never synthesises a preview at runtime: a bundled clip
is instant, works offline, and costs nothing per tap.

    cd backend
    python -m src.agent.generate_voice_previews

Clips are rendered at PREVIEW_SPEED, the centre of voice_controls.TONE_TO_SPEED,
so what a user auditions matches what a real call sounds like. An unconditioned
preview would be noticeably brisker than Buddy actually speaks.

Every voice speaks its own line (PREVIEW_LINES). What each clip was rendered from
is recorded in assets/voices/previews.json, so editing a line re-renders that one
clip and leaves the rest alone. That file is bundled with the app because the
whole assets/voices/ directory is; it is a few hundred bytes and nothing reads it
at runtime.

The clips currently shipped predate that manifest and were approved by ear, so
their exact transcripts are not known here and PREVIEW_LINES does not claim to
reproduce them. A clip with no manifest entry is therefore left alone: delete the
mp3 to ask for a fresh render.

This file replaces generate_filler_audio.py, which rendered four thinking-state
filler clips ("Hmmm.", "Sure.") in the then-hardcoded default voice. That output
had no consumers anywhere in the app or backend — the live filler path speaks
through session.say() (voice/tool_filler.py) and so already follows the selected
voice. If pre-rendered fillers are ever revived they must be generated per voice,
not once, or they will contradict whichever voice the user picked.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

from .voice.voice_catalog import CATALOG

# Repo root: backend/src/agent -> backend/src -> backend -> root.
_ASSET_DIR = Path(__file__).resolve().parents[3] / "assets" / "voices"

# What each clip on disk was rendered from. Without this the "already exists"
# skip below is permanent: editing a line would leave all eight stale clips in
# place and the change would silently do nothing.
_MANIFEST = _ASSET_DIR / "previews.json"

_MODEL = "sonic-3.5"
_API_VERSION = "2025-04-16"

# Centre of voice_controls.TONE_TO_SPEED (0.90-0.95).
PREVIEW_SPEED = 0.92

# The first thing a user hears from each voice. Every voice gets its OWN line:
# eight readings of one sentence is a TTS bake-off, and the picker is supposed
# to feel like meeting eight people. Each has to sound like Buddy picking up a
# thread mid-friendship, and should play to that voice's character.
PREVIEW_LINES: dict[str, str] = {
    # The incumbent voice keeps the incumbent line.
    "katie": "Hey, it's me. I was just thinking about what you said yesterday.",
    "dallas": "Alright, I'm here. Take your time, I'm not going anywhere.",
    "tessa": "There you are. I was hoping you'd call tonight.",
    "kira": "Okay. Just tell me what happened, and we'll figure it out.",
    "layla": "No rush. We can sit with this one for a minute.",
    "jolene": "Well, hey you. Come on now, tell me everything.",
    "kyle": "Oh, this is going to be good. Go on, I'm listening.",
    "archie": "Right then. Shall we get into it, or are we pretending everything's fine?",
}


def _load_manifest() -> dict[str, dict]:
    if not _MANIFEST.exists():
        return {}
    try:
        return json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt manifest must re-render everything, not skip everything.
        print(f"  manifest unreadable ({exc}); treating every clip as stale")
        return {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    _MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _generate(
    api_key: str,
    voice_slug: str,
    cartesia_voice_id: str,
    manifest: dict[str, dict],
) -> None:
    dest = _ASSET_DIR / f"{voice_slug}.mp3"
    line = PREVIEW_LINES[voice_slug]
    recorded = manifest.get(voice_slug)

    if dest.exists():
        if recorded is None:
            # A clip with no manifest entry predates this manifest. Its transcript
            # is unknown, and the shipped clips are ones Varun has listened to and
            # approved, so this must NOT assume they match PREVIEW_LINES and
            # re-render over them. Deleting the file asks for a new one.
            print(f"  keep  {dest.name}  (pre-manifest clip, delete it to re-render)")
            return
        if recorded.get("line") == line and recorded.get("speed") == PREVIEW_SPEED:
            print(f"  skip  {dest.name}  (unchanged)")
            return
        print(f"  stale {dest.name}  (line or speed changed, re-rendering)")

    payload = {
        "model_id": _MODEL,
        "transcript": line,
        "voice": {"mode": "id", "id": cartesia_voice_id},
        "speed": PREVIEW_SPEED,
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
    }

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=30, http2=False) as client:
                resp = client.post(
                    "https://api.cartesia.ai/tts/bytes",
                    headers={
                        "Cartesia-Version": _API_VERSION,
                        "X-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            # Print the body before raising. A 400 here means the request shape is
            # wrong (most likely `speed`, whose REST contract differs from the
            # LiveKit plugin kwarg) and retrying three times while swallowing the
            # reason wastes the only signal that would fix it.
            if resp.status_code >= 400:
                print(f"  HTTP {resp.status_code} for {voice_slug}: {resp.text[:400]}")
                if resp.status_code < 500:
                    raise RuntimeError(f"Cartesia rejected the request for {voice_slug}")
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            # Record only after the bytes land, so a crash mid-run leaves the
            # clip stale rather than marked current.
            manifest[voice_slug] = {"line": line, "speed": PREVIEW_SPEED}
            _save_manifest(manifest)
            print(f"  wrote {dest.name}  ({len(resp.content) / 1024:.1f} KB)")
            return
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            print(f"  attempt {attempt} failed: {type(exc).__name__}: {exc} — retrying in {attempt}s")
            time.sleep(attempt)

    raise RuntimeError(f"Failed after 3 attempts for {voice_slug}: {last_error}")


def _resolve_api_key() -> str:
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if api_key:
        return api_key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("CARTESIA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> None:
    # Fail before the first API call, not halfway through a paid run. A catalog
    # entry with no line would otherwise render seven clips and then die.
    missing = [voice.slug for voice in CATALOG if voice.slug not in PREVIEW_LINES]
    if missing:
        print(f"ERROR: no PREVIEW_LINES entry for: {', '.join(missing)}")
        sys.exit(1)

    api_key = _resolve_api_key()
    if not api_key:
        print("ERROR: CARTESIA_API_KEY not found in environment or backend/.env")
        sys.exit(1)

    _ASSET_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    print(f"Rendering {len(CATALOG)} preview clips into {_ASSET_DIR}")
    print(f"  speed: {PREVIEW_SPEED}\n")

    for voice in CATALOG:
        print(f"  {voice.slug}: {PREVIEW_LINES[voice.slug]!r}")
        _generate(api_key, voice.slug, voice.cartesia_voice_id, manifest)

    print("\nDone. Listen to every clip before committing them.")


if __name__ == "__main__":
    main()
