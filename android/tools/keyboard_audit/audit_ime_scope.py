#!/usr/bin/env python3
"""Fail-closed source audit for Aura IME key-path and hard voice boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


VOICE_PATHS = (
    "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceController.kt",
    "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceHandoff.kt",
    "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardAudioHandler.kt",
    "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/VoiceWaveformView.kt",
    "android/app/src/main/kotlin/dev/varuntej/aura/assistant/",
    "android/app/src/main/kotlin/dev/varuntej/aura/widget/VoiceWidgetProvider.kt",
    "android/app/src/main/kotlin/dev/varuntej/aura/widget/VoiceTileService.kt",
    "lib/core/voice/",
    "lib/data/services/voice_session_service.dart",
    "lib/data/services/voice_launcher_bridge.dart",
    "lib/data/services/wake_word_service.dart",
    "lib/presentation/widgets/voice_waveform.dart",
    "lib/presentation/widgets/voice_sphere.dart",
    "backend/src/agent/voice/",
    "backend/src/agent/voice_agent.py",
    "backend/src/agent/voice_prompt.py",
    "backend/src/handlers/realtime.py",
    "backend/src/handlers/dictation.py",
    "backend/src/services/dictation/",
)
VOICE_METHODS = (
    "openVoice", "startVoiceSession", "handoffToAppVoice", "renderVoice", "ensureVoiceStage",
    "buildVoiceStage", "teardownVoiceStage", "onVoiceTranscript", "stopVoice", "launchAppVoice",
)
BUDDY_PATH = "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt"
PROTECTED_UNCHANGED_FILES = (
    "android/app/src/main/AndroidManifest.xml",
    "android/app/src/main/res/xml/method.xml",
    "android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardDraftClient.kt",
)
VOICE_NAMED_PATH_TOKENS = (
    "voice", "audio", "livekit", "webrtc", "wake_word", "realtime", "dictation",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, encoding="utf-8", errors="strict",
        capture_output=True,
    ).stdout


def member(source: str, name: str) -> str:
    start_match = re.search(rf"(?m)^    (?:private |public |internal |protected )?"
                            rf"(?:suspend )?fun {re.escape(name)}\b", source)
    if not start_match:
        raise ValueError(f"member not found: {name}")
    next_member = re.search(
        r"(?m)^    (?:private |public |internal |protected |override )"
        r"(?:val |var |fun |class |data class |object )",
        source[start_match.end():],
    )
    end = start_match.end() + next_member.start() if next_member else len(source)
    return source[start_match.start():end].replace("\r\n", "\n").rstrip()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized(source: str) -> str:
    return source.replace("\r\n", "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    changed = set(git(repo, "diff", "--name-only", args.base, "--").splitlines())
    voice_path_changes = sorted(
        path for path in changed if any(path == excluded or path.startswith(excluded)
                                        for excluded in VOICE_PATHS)
    )
    voice_named_path_changes = sorted(
        path for path in changed
        if any(token in path.lower() for token in VOICE_NAMED_PATH_TOKENS)
    )
    base_buddy = git(repo, "show", f"{args.base}:{BUDDY_PATH}")
    current_buddy = (repo / BUDDY_PATH).read_text(encoding="utf-8")
    method_hashes = {}
    for name in VOICE_METHODS:
        before = sha(member(base_buddy, name))
        after = sha(member(current_buddy, name))
        method_hashes[name] = {"base_sha256": before, "current_sha256": after, "equal": before == after}
    base_voice_lines = "\n".join(
        line for line in normalized(base_buddy).splitlines() if "voice" in line.lower()
    )
    current_voice_lines = "\n".join(
        line for line in normalized(current_buddy).splitlines() if "voice" in line.lower()
    )
    protected_files = {}
    for path in PROTECTED_UNCHANGED_FILES:
        before = normalized(git(repo, "show", f"{args.base}:{path}"))
        after = normalized((repo / path).read_text(encoding="utf-8"))
        protected_files[path] = {
            "base_sha256": sha(before),
            "current_sha256": sha(after),
            "equal": before == after,
        }

    commit_char = member(current_buddy, "commitChar")
    update_predictions = member(current_buddy, "updatePredictions")
    separator = member(current_buddy, "flushComposingWord") + member(current_buddy, "commitSeparator")
    ordinary_banned = (
        "getTextBeforeCursor", "getTextAfterCursor", "getSurroundingText", "SQLite", "File(",
        "SharedPreferences", "VocabHintsCache", "Firestore", "http", "await(", "Future", "sleep(",
    )
    prediction_publication_banned = (
        "Handler", "postDelayed", "Executor", "execute(", "submit(", "sleep(",
    )
    separator_banned = (
        "BaseDictionary.corrections", "SpellChecker", "Autocorrector", "editDistance",
    )
    key_path_findings = {
        "ordinary_letter_banned_tokens": sorted(token for token in ordinary_banned
                                                  if token in commit_char),
        "prediction_publication_banned_tokens": sorted(token for token in prediction_publication_banned
                                                        if token in update_predictions and
                                                        token != "submit("),
        "separator_traversal_tokens": sorted(token for token in separator_banned if token in separator),
        "automatic_vocab_hints_reference": "VocabHintsCache" in current_buddy,
    }
    base_gradle = git(repo, "show", f"{args.base}:android/app/build.gradle.kts")
    current_gradle = (repo / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    base_voice_dependencies = [line.strip() for line in base_gradle.splitlines()
                               if "livekit" in line.lower() or "webrtc" in line.lower()]
    current_voice_dependencies = [line.strip() for line in current_gradle.splitlines()
                                  if "livekit" in line.lower() or "webrtc" in line.lower()]
    report = {
        "base": args.base,
        "voice_excluded_path_changes": voice_path_changes,
        "voice_named_path_changes": voice_named_path_changes,
        "buddy_voice_lines_equal": base_voice_lines == current_voice_lines,
        "protected_voice_methods": method_hashes,
        "protected_manifest_and_drafting_files": protected_files,
        "voice_dependency_lines_equal": base_voice_dependencies == current_voice_dependencies,
        "key_path": key_path_findings,
    }
    passed = (
        not voice_path_changes and
        not voice_named_path_changes and
        report["buddy_voice_lines_equal"] and
        all(entry["equal"] for entry in method_hashes.values()) and
        all(entry["equal"] for entry in protected_files.values()) and
        report["voice_dependency_lines_equal"] and
        all(not value for value in key_path_findings.values())
    )
    report["passed"] = passed
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
