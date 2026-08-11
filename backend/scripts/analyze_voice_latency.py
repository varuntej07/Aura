"""Stage 0 latency report: what actually kills preemptive generation, and where
the end-to-end milliseconds go.

Read-only. Consumes captured worker logs; touches no service and no user data.

WHY THIS EXISTS
---------------
Desktop turns measure 2.4-3.0s against a <=1.5s target, and the cause was being
argued from code reading rather than measured. Every number needed to settle it
is already emitted per turn and had never been read:

  VoiceTurn: metrics                          turn-keyed, carries frame_attached
  VoiceLatency: preemptive generation decision  which mutation discarded reuse
  VoiceLatency: speculative outcome             intended vs OBSERVED reuse
  VoiceSession: turn metrics                    endpointing / hook / playback

Capture a session, then run this. It is meant to be run three times against the
same command: once for the baseline, once after graph retrieval moves off the
finalization hook, once after early frame injection. The comparison is the whole
point, so the output is deliberately stable and diff-friendly.

    lk agent logs --id <AGENT_ID> > session.jsonl     # Ctrl-C when the call ends
    cd backend && python scripts/analyze_voice_latency.py session.jsonl

`lk agent logs` tails live and never exits on its own; redirect it, make the
call, then stop it.

READING THE OUTPUT
------------------
`decision` names the ONE mutation that invalidated a turn's speculative reply
(voice/speculation.py collapses several to the most consequential). `unchanged`
is the only reusable value. The leader of that distribution is the thing worth
fixing; everything else is noise until it is gone.

`intended` vs `observed` reuse is not redundant. The hook can intend reuse and
LiveKit can still discard it, so a gap between these two columns means the
invalidation is happening somewhere this codebase does not control.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# The four log lines this report is built from. Names are matched exactly:
# lib/logger.py flattens the metadata dict onto the record alongside "message",
# so every field below is top-level in the JSON.
_TURN_METRICS = "VoiceTurn: metrics"
_DECISION = "VoiceLatency: preemptive generation decision"
_OUTCOME = "VoiceLatency: speculative outcome"
_SESSION_TURN = "VoiceSession: turn metrics"

# Mirrors preemptive_generation["max_retries"] in voice/pipelines.py. Kept here
# rather than imported so this script stays runnable against a capture from a
# worker build whose config has since changed; update both together.
_PREEMPTIVE_MAX_RETRIES = 6

# Component timings, in the order they occur within a turn, so the printed table
# reads as the actual critical path rather than alphabetically.
_COMPONENTS = (
    ("endpointing_ms", "endpointing (VAD stop -> turn commit)"),
    ("stt_final_ms", "stt finalize"),
    ("turn_hook_ms", "on_user_turn_completed hook"),
    ("llm_ttft_ms", "llm time to first token"),
    ("tts_ttfb_ms", "tts time to first byte"),
    ("playback_ms", "playback buffer"),
    ("eou_to_first_audio_ms", "END TO END (user silent -> buddy audible)"),
)


@dataclass
class Turn:
    """One turn, joined across log lines by (session_id, turn_index)."""

    frame_attached: bool | None = None
    resolved_model: str | None = None
    fallback_occurred: bool | None = None
    model_requests: int | None = None
    decision: str | None = None
    intended_reuse: bool | None = None
    observed_reuse: bool | None = None
    interim_updates: int | None = None
    transcript_churned: bool | None = None
    timings: dict[str, int] = field(default_factory=dict)


def _decode(raw: bytes) -> list[str]:
    """Decode a captured log file whatever encoding the shell wrote it in.

    Not optional on Windows. `lk agent logs > file.jsonl` under Windows
    PowerShell 5.1 writes UTF-16LE, because `>` is an alias for Out-File whose
    documented default is Unicode, not UTF-8. Reading that as UTF-8 turns every
    line into mojibake, and the parser then reports "no voice turns found" for a
    capture that is perfectly intact. Sniff the BOM instead of trusting a
    default, so the same file works from PowerShell, pwsh, bash and a pipe.
    """
    for bom, encoding in (
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace").splitlines()
    return raw.decode("utf-8", errors="replace").splitlines()


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. Deliberately not interpolating: these samples are
    small (one call is tens of turns) and an interpolated p95 over 12 points
    invents a number no turn actually took."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def parse(lines: list[str]) -> tuple[dict[tuple[str, int], Turn], dict[str, list[float]]]:
    """Join turn-keyed lines, and collect session-level timings separately.

    Two collections rather than one because they key differently. `VoiceTurn:
    metrics` and both VoiceLatency lines carry turn_index and can be joined.
    `VoiceSession: turn metrics` does NOT carry one (it is emitted straight off
    the SDK metrics callback), so endpointing/hook/playback can only be
    aggregated across the session, never attributed to an individual turn. Faking
    that join by arrival order would silently mis-attribute on any turn that was
    interrupted or reordered, so it is not attempted.
    """
    turns: dict[tuple[str, int], Turn] = defaultdict(Turn)
    session_timings: dict[str, list[float]] = defaultdict(list)

    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue  # lk prints config warnings and plain status lines too
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        message = record.get("message")
        session_id = record.get("session_id")

        if message == _SESSION_TURN:
            for key, _ in _COMPONENTS:
                value = record.get(key)
                if isinstance(value, (int, float)):
                    session_timings[key].append(float(value))
            continue

        index = record.get("turn_index")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        turn = turns[(session_id, index)]

        if message == _TURN_METRICS:
            turn.frame_attached = record.get("frame_attached")
            turn.resolved_model = record.get("resolved_model")
            turn.fallback_occurred = record.get("fallback_occurred")
            turn.model_requests = record.get("n_model_requests_this_turn")
            for key in (
                "t_stt_final_ms",
                "t_model_first_chunk_ms",
                "t_tts_first_byte_ms",
                "t_end_to_end_first_audio_ms",
            ):
                value = record.get(key)
                if isinstance(value, (int, float)):
                    turn.timings[key] = int(value)
        elif message == _DECISION:
            turn.decision = record.get("decision")
            turn.interim_updates = record.get("interim_updates")
            turn.transcript_churned = record.get("transcript_churned")
        elif message == _OUTCOME:
            turn.intended_reuse = record.get("intended_reuse")
            turn.observed_reuse = record.get("observed_reuse")

    return dict(turns), dict(session_timings)


def _print_distribution(title: str, counter: Counter, total: int) -> None:
    print(f"\n{title}")
    if not counter:
        print("  (no data)")
        return
    for name, count in counter.most_common():
        share = 100.0 * count / total if total else 0.0
        print(f"  {str(name):<34} {count:>4}  {share:5.1f}%")


def report(turns: dict[tuple[str, int], Turn], session_timings: dict[str, list[float]]) -> int:
    if not turns and not session_timings:
        print(
            "No voice turns found.\n\n"
            "The worker only writes these lines while a call is in progress, and\n"
            "`lk agent logs` shows the CURRENT deployment only, and a redeploy drops\n"
            "prior sessions. Capture again during a live call.",
            file=sys.stderr,
        )
        return 1

    total = len(turns)
    print("=" * 74)
    print(f"STAGE 0 VOICE LATENCY REPORT   turns={total}   sessions="
          f"{len({session for session, _ in turns})}")
    print("=" * 74)

    _print_distribution(
        "SPECULATION DECISION  (only `unchanged` reuses the preemptive reply)",
        Counter(turn.decision for turn in turns.values() if turn.decision),
        total,
    )

    intended = sum(1 for t in turns.values() if t.intended_reuse)
    observed = sum(1 for t in turns.values() if t.observed_reuse)
    resolved = sum(1 for t in turns.values() if t.observed_reuse is not None)
    print("\nREUSE")
    print(f"  intended reuse                     {intended:>4}  "
          f"{100.0 * intended / total if total else 0:5.1f}%")
    print(f"  OBSERVED reuse                     {observed:>4}  "
          f"{100.0 * observed / total if total else 0:5.1f}%")
    if resolved and observed < intended:
        lost = [t for t in turns.values() if t.intended_reuse and not t.observed_reuse]
        churned = sum(1 for t in lost if t.transcript_churned)
        # Compared against the configured preemptive max_retries. Past it the
        # speculation stops refreshing while the user keeps talking, so the last
        # speculated transcript can never match the finalized one.
        exhausted = sum(1 for t in lost if (t.interim_updates or 0) > _PREEMPTIVE_MAX_RETRIES)
        print(f"  !! {intended - observed} turn(s) intended reuse and did not get it.")
        print(f"     of those, transcript churned:        {churned}")
        print(f"     of those, interim updates > {_PREEMPTIVE_MAX_RETRIES:<2}:      {exhausted}")
        unexplained = len(lost) - sum(
            1 for t in lost if t.transcript_churned or (t.interim_updates or 0) > _PREEMPTIVE_MAX_RETRIES
        )
        if unexplained:
            print(f"     STILL UNEXPLAINED:                  {unexplained}")
            print("     Neither the transcript nor retry exhaustion accounts for these.")
            print("     Suspect a context mutation from a background injection task")
            print("     landing after the last speculation snapshot.")

    armed = [t for t in turns.values() if t.frame_attached]
    unarmed = [t for t in turns.values() if t.frame_attached is False]
    print(f"\nSCREEN  armed={len(armed)}  unarmed={len(unarmed)}")
    if armed:
        armed_reuse = sum(1 for t in armed if t.observed_reuse)
        print(f"  reuse on armed turns               {armed_reuse:>4}  "
              f"{100.0 * armed_reuse / len(armed):5.1f}%")
    if unarmed:
        unarmed_reuse = sum(1 for t in unarmed if t.observed_reuse)
        print(f"  reuse on unarmed turns             {unarmed_reuse:>4}  "
              f"{100.0 * unarmed_reuse / len(unarmed):5.1f}%")

    print("\nFALLBACK  (every failover is audible dead air)")
    fell_back = sum(1 for t in turns.values() if t.fallback_occurred)
    print(f"  turns that left the primary leg    {fell_back:>4}  "
          f"{100.0 * fell_back / total if total else 0:5.1f}%")
    _print_distribution("  resolved model", Counter(
        t.resolved_model for t in turns.values() if t.resolved_model), total)

    print("\nCOMPONENT LATENCY (ms)")
    print(f"  {'component':<44} {'p50':>6} {'p95':>6} {'n':>5}")
    for key, label in _COMPONENTS:
        values = session_timings.get(key, [])
        print(f"  {label:<44} {_fmt(_percentile(values, 0.50)):>6} "
              f"{_fmt(_percentile(values, 0.95)):>6} {len(values):>5}")

    print("\nARMED vs UNARMED  (from turn-keyed metrics)")
    print(f"  {'metric':<44} {'armed':>6} {'unarm':>6}")
    for key, label in (
        ("t_model_first_chunk_ms", "llm time to first token"),
        ("t_tts_first_byte_ms", "tts time to first byte"),
        ("t_end_to_end_first_audio_ms", "END TO END"),
    ):
        a = _percentile([t.timings[key] for t in armed if key in t.timings], 0.50)
        u = _percentile([t.timings[key] for t in unarmed if key in t.timings], 0.50)
        print(f"  {label + ' p50':<44} {_fmt(a):>6} {_fmt(u):>6}")

    print("\n" + "-" * 74)
    print("Leader of the decision distribution is what to fix first.")
    print("-" * 74)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage 0 voice latency report from captured worker logs.",
    )
    parser.add_argument(
        "logfile",
        nargs="?",
        help="Captured `lk agent logs` output. Reads stdin when omitted.",
    )
    args = parser.parse_args()

    if args.logfile:
        with open(args.logfile, "rb") as handle:
            lines = _decode(handle.read())
    else:
        lines = _decode(sys.stdin.buffer.read())

    turns, session_timings = parse(lines)
    return report(turns, session_timings)


if __name__ == "__main__":
    raise SystemExit(main())
