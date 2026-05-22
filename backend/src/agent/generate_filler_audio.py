"""
One-time script: generate vocal filler audio clips for the voice agent thinking state.

Uses the Cartesia HTTP API (same voice as Buddy's live TTS) to produce four short
WAV clips. Run once locally, then commit the generated files under src/agent/audio/.

Usage:
    cd backend
    python -m src.agent.generate_filler_audio
"""

import os
import sys
import time
from pathlib import Path

import httpx

_AUDIO_DIR = Path(__file__).parent / "audio"

# Default Cartesia voice used by the LiveKit cartesia.TTS plugin when no voice is specified.
_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"
_MODEL = "sonic-3"
_API_VERSION = "2025-04-16"

_CLIPS: list[tuple[str, str]] = [
    ("filler_hmm.wav", "Hmmm."),
    ("filler_ah.wav", "Ahhaa."),
    ("filler_sure.wav", "Sure."),
    ("filler_let_me_see.wav", "Lemme see."),
]


def _generate(api_key: str, filename: str, text: str) -> None:
    dest = _AUDIO_DIR / filename
    if dest.exists():
        print(f"  skip  {filename}  (already exists)")
        return

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
                    json={
                        "model_id": _MODEL,
                        "transcript": text,
                        "voice": {"mode": "id", "id": _VOICE_ID},
                        "output_format": {
                            "container": "wav",
                            "encoding": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                    },
                )
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            size_kb = len(resp.content) / 1024
            print(f"  wrote {filename}  ({size_kb:.1f} KB)  text='{text}'")
            return
        except Exception as exc:
            last_error = exc
            print(f"  attempt {attempt} failed: {type(exc).__name__} — retrying in {attempt}s")
            time.sleep(attempt)

    raise RuntimeError(f"Failed after 3 attempts: {last_error}")


def main() -> None:
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if not api_key:
        # Fall back to .env file in backend/
        env_path = Path(__file__).parent.parent.parent / ".env"

        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("CARTESIA_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not api_key:
        print("ERROR: CARTESIA_API_KEY not found in environment or .env")
        sys.exit(1)

    _AUDIO_DIR.mkdir(exist_ok=True)
    print(f"Generating {len(_CLIPS)} filler clips into {_AUDIO_DIR}")

    for filename, text in _CLIPS:
        _generate(api_key, filename, text)

    print("Done. Commit the generated .wav files.")


if __name__ == "__main__":
    main()
