"""Export retained dictation traces as a NeMo ASR corpus.

Run from ``backend`` with application-default credentials or the local service
account configured:

    python scripts/export_dictation_corpus.py --output ./dictation-corpus

The export refuses to overwrite a non-empty directory. Audio paths in the
manifest are relative, and the stable ``speaker_id`` is an HMAC pseudonym so
train/validation splits can stay user-disjoint without exporting Firebase UIDs.
Set ``DICTATION_EXPORT_HMAC_KEY`` to a secret value before running.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from src.services.dictation import fields as F  # noqa: E402
from src.services.dictation import gcs_audio, store  # noqa: E402
from src.services.firebase import admin_firestore  # noqa: E402


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _speaker_id(uid: str, key: bytes) -> str:
    return hmac.new(key, uid.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _v2(trace: dict) -> bool:
    return int(trace.get("schemaVersion") or 1) >= 2


def _asr_text(trace: dict) -> str:
    """The transcript as ASR produced it, before any correction.

    V1 called this asrText; V2 renamed it rawTranscript and dropped app,
    fieldRole and modelId entirely. Reading the V1 names unconditionally made
    every V2 document either KeyError here or, worse, vanish silently at the
    groundTruth check below, since V2 has no such field.
    """
    return trace["rawTranscript"] if _v2(trace) else trace["asrText"]


def _training_text(trace: dict) -> str | None:
    """What the corpus should treat as the label.

    V2 states it directly as trainingText, which the client computed. V1 only
    has it when the user corrected the text, which is why it can be absent and
    why an absent value still means "skip", not "use the ASR output".
    """
    return trace.get("trainingText") if _v2(trace) else trace.get("groundTruth")


def _model_id(trace: dict) -> str:
    return trace["providerModel"] if _v2(trace) else trace["modelId"]


def _edit_line(trace: dict, edit: dict) -> dict[str, Any]:
    return {
        "trace_id": trace[F.TRACE_ID],
        "class": edit["class"],
        "from": edit["from"],
        "to": edit["to"],
        "word_index": edit["wordIndex"],
        "asr_text": _asr_text(trace),
        "inserted_text": trace["insertedText"],
        "final_text": trace["finalText"],
        "app": trace.get("app"),
    }


async def export(output: Path, identity_key: bytes) -> dict[str, int]:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"Refusing to overwrite non-empty directory: {output}")
    audio_root = output / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)

    snapshots = await asyncio.to_thread(
        lambda: list(admin_firestore().collection_group(F.TRACE_SUBCOLLECTION).stream())
    )
    manifest: list[str] = []
    corrections: list[str] = []
    style_edits: list[str] = []
    skipped = 0

    for snapshot in snapshots:
        trace = snapshot.to_dict() or {}
        if trace.get(F.DELETED_AT) or trace.get(F.DELETION_STATE):
            continue
        trace_id = str(trace.get(F.TRACE_ID) or snapshot.id)
        trace[F.TRACE_ID] = trace_id

        for edit in trace.get("edits", []):
            if not isinstance(edit, dict) or edit.get("class") not in F.EDIT_CLASSES:
                continue
            line = _json_line(_edit_line(trace, edit))
            if edit["class"] in F.GROUND_TRUTH_EDIT_CLASSES:
                corrections.append(line)
            else:
                style_edits.append(line)

        path = trace.get(F.AUDIO_PATH)
        generation = trace.get(F.AUDIO_GENERATION)
        ground_truth = _training_text(trace)
        if not trace.get(F.HAS_AUDIO) or not path or generation is None or not ground_truth:
            skipped += 1
            continue

        try:
            audio = await gcs_audio.download_exact(path, str(generation))
        except Exception as exc:
            from google.api_core.exceptions import NotFound  # type: ignore

            if not isinstance(exc, NotFound):
                raise
            await store.mark_audio_missing(
                snapshot.reference,
                path=path,
                generation=str(generation),
            )
            skipped += 1
            continue

        expected_digest = str(trace.get("audioSha256") or "")
        if hashlib.sha256(audio).hexdigest() != expected_digest:
            raise RuntimeError(f"Digest mismatch for immutable trace {trace_id}")

        user_ref = snapshot.reference.parent.parent
        if user_ref is None:
            raise RuntimeError(f"Unexpected Firestore path for trace {trace_id}")
        speaker_id = _speaker_id(user_ref.id, identity_key)
        relative_path = Path("audio") / speaker_id / f"{trace_id}.flac"
        destination = output / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio)

        manifest.append(
            _json_line(
                {
                    "audio_filepath": relative_path.as_posix(),
                    "duration": int(trace["durationMs"]) / 1_000,
                    "text": ground_truth,
                    "trace_id": trace_id,
                    "speaker_id": speaker_id,
                    "asr_text": _asr_text(trace),
                    "inserted_text": trace["insertedText"],
                    "app": trace.get("app"),
                    "field_role": trace.get("fieldRole"),
                    "model_id": _model_id(trace),
                    "recorded_at_ms": trace["recordedAtMs"],
                    "consent_version": trace["consentVersion"],
                    "verified": True,
                }
            )
        )

    (output / "manifest.jsonl").write_text("".join(manifest), encoding="utf-8")
    (output / "corrections.jsonl").write_text("".join(corrections), encoding="utf-8")
    (output / "style_edits.jsonl").write_text("".join(style_edits), encoding="utf-8")
    return {
        "documents": len(snapshots),
        "manifest_lines": len(manifest),
        "correction_edits": len(corrections),
        "style_edits": len(style_edits),
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw_key = os.getenv("DICTATION_EXPORT_HMAC_KEY", "")
    if len(raw_key) < 32:
        raise SystemExit("DICTATION_EXPORT_HMAC_KEY must be set to at least 32 characters.")
    result = asyncio.run(export(args.output.resolve(), raw_key.encode("utf-8")))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

