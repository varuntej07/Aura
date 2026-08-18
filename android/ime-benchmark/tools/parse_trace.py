#!/usr/bin/env python3
"""Extract release-to-commit, presentation, suggestion, correctness, and ONNX evidence."""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path


NULL_TEXT_VALUES = {"", "[NULL]", "NULL", "None", "NONE"}


EVENT_LATENCY_SQL = r"""
WITH
releases AS (
  SELECT row_number() OVER (ORDER BY ts) AS event_index, ts AS release_ts
  FROM slice WHERE name = 'AuraBench:injected ACTION_UP'
),
mutations AS (
  SELECT c.ts, CAST(c.value AS INT) AS event_index
  FROM counter c JOIN track t ON c.track_id = t.id
  WHERE t.name = 'AuraBench text mutation event' AND c.value > 0
),
draw_values AS (
  SELECT c.ts, CAST(c.value AS INT) AS event_index,
         lag(CAST(c.value AS INT), 1, 0) OVER (ORDER BY c.ts) AS previous_event
  FROM counter c JOIN track t ON c.track_id = t.id
  WHERE t.name = 'AuraBench drawn event'
),
draws AS (
  SELECT ts, event_index FROM draw_values
  WHERE event_index > previous_event AND event_index > 0
),
mapped AS (
  SELECT r.event_index, r.release_ts,
         (SELECT min(m.ts) FROM mutations m WHERE m.event_index = r.event_index) AS commit_ts,
         (SELECT count(*) FROM mutations m WHERE m.event_index = r.event_index) AS mutation_count,
         (SELECT min(d.ts) FROM draws d WHERE d.event_index >= r.event_index) AS draw_ts
  FROM releases r
),
presented AS (
  SELECT m.*,
         (SELECT min(f.ts + f.dur)
          FROM actual_frame_timeline_slice f
          WHERE f.layer_name GLOB '*dev.varuntej.aura.imebenchmark*'
            AND f.ts <= m.draw_ts AND f.ts + f.dur >= m.draw_ts) AS present_ts
  FROM mapped m
)
SELECT event_index, release_ts, commit_ts, mutation_count, draw_ts, present_ts,
       (commit_ts - release_ts) / 1000000.0 AS release_to_commit_ms,
       (present_ts - commit_ts) / 1000000.0 AS commit_to_present_ms,
       (present_ts - release_ts) / 1000000.0 AS release_to_present_ms
FROM presented ORDER BY event_index;
"""

MUTATION_ORDER_SQL = r"""
SELECT c.ts, CAST(c.value AS INT) AS event_index
FROM counter c JOIN track t ON c.track_id = t.id
WHERE t.name = 'AuraBench text mutation event' AND c.value > 0
ORDER BY c.ts;
"""

RUNTIME_SQL = r"""
WITH bounds AS (
  SELECT min(ts) AS first_key_ts, max(ts) AS last_key_ts
  FROM slice WHERE name='AuraBench:injected ACTION_UP'
)
SELECT
  (SELECT count(*) FROM slice s JOIN bounds b
     WHERE s.name = 'AuraIme:ORT inference'
       AND s.ts BETWEEN b.first_key_ts AND b.last_key_ts) AS ort_inference_slices,
  (SELECT count(*) FROM slice WHERE name = 'AuraIme:ORT initialize+warm') AS ort_warm_count,
  (SELECT max(value) FROM counter c JOIN track t ON c.track_id=t.id
     WHERE t.name='AuraIme ORT inference count') AS ort_diagnostic_inference_count,
  (SELECT max(value) FROM counter c JOIN track t ON c.track_id=t.id
     WHERE t.name='AuraIme prediction pending') AS max_prediction_pending,
  (SELECT max(value) FROM counter c JOIN track t ON c.track_id=t.id
     WHERE t.name='AuraIme prediction active') AS max_prediction_active,
  (SELECT count(*) FROM slice s JOIN bounds b
     WHERE s.name = 'AuraIme:InputConnection mutation'
       AND s.ts BETWEEN b.first_key_ts AND b.last_key_ts) AS input_mutations,
  (SELECT count(*) FROM slice s
      JOIN thread_track tt ON s.track_id=tt.id
      JOIN thread t ON tt.utid=t.utid JOIN process p ON t.upid=p.upid
      JOIN bounds b
      WHERE p.name GLOB '__IME_PACKAGE__*'
        AND s.ts BETWEEN b.first_key_ts AND b.last_key_ts
        AND lower(s.name) GLOB '*gc*') AS gc_slice_count,
  (SELECT count(*) FROM ftrace_event f
      JOIN thread t ON f.utid=t.utid JOIN process p ON t.upid=p.upid
      JOIN bounds b
      WHERE p.name GLOB '__IME_PACKAGE__*'
        AND f.ts BETWEEN b.first_key_ts AND b.last_key_ts
        AND f.name IN ('block_rq_issue', 'ext4_da_write_begin', 'f2fs_write_begin'))
      AS traced_disk_events,
  (SELECT count(*) FROM ftrace_event f
      JOIN thread t ON f.utid=t.utid JOIN process p ON t.upid=p.upid
      JOIN bounds b
      WHERE p.name GLOB '__IME_PACKAGE__*'
        AND f.ts BETWEEN b.first_key_ts AND b.last_key_ts
        AND f.name IN ('net_dev_queue', 'netif_receive_skb')) AS traced_network_events;
"""

ORT_LABEL_SQL = r"""
SELECT name FROM slice
WHERE name GLOB 'AuraIme:ORT state:*'
   OR name GLOB 'AuraIme:ORT provider:*'
   OR name GLOB 'AuraIme:ORT runtime:*'
   OR name GLOB 'AuraIme:ORT failure:*'
ORDER BY ts;
"""

SUGGESTION_SQL = r"""
SELECT t.name AS category, c.value / 1000.0 AS latency_ms
FROM counter c JOIN track t ON c.track_id=t.id
WHERE t.name IN (
  'AuraIme suggestion lexical latency us',
  'AuraIme suggestion deferred latency us'
)
ORDER BY c.ts;
"""

INFERENCE_SQL = r"""
WITH bounds AS (
  SELECT min(ts) AS first_key_ts, max(ts) AS last_key_ts
  FROM slice WHERE name='AuraBench:injected ACTION_UP'
)
SELECT s.dur / 1000000.0 AS inference_ms
FROM slice s JOIN bounds b
WHERE s.name='AuraIme:ORT inference' AND s.dur >= 0
  AND s.ts BETWEEN b.first_key_ts AND b.last_key_ts
ORDER BY s.ts;
"""

DURATION_SQL = r"""
WITH bounds AS (
  SELECT min(ts) AS first_key_ts, max(ts) AS last_key_ts
  FROM slice WHERE name='AuraBench:injected ACTION_UP'
)
SELECT s.name AS category, s.dur / 1000000.0 AS duration_ms
FROM slice s JOIN bounds b
WHERE s.dur >= 0 AND s.ts BETWEEN b.first_key_ts AND b.last_key_ts
  AND s.name IN (
    'AuraIme:key handler',
    'AuraIme:InputConnection mutation',
    'AuraIme:lexical prediction',
    'AuraIme:deferred prediction',
    'AuraBench:glyph draw'
  )
ORDER BY s.ts;
"""

FRAME_SQL = r"""
WITH bounds AS (
  SELECT min(ts) AS first_key_ts, max(ts) AS last_key_ts
  FROM slice WHERE name='AuraBench:injected ACTION_UP'
)
SELECT f.dur / 1000000.0 AS frame_ms, f.jank_type
FROM actual_frame_timeline_slice f JOIN bounds b
WHERE f.layer_name GLOB '*dev.varuntej.aura.imebenchmark*'
  AND f.ts BETWEEN b.first_key_ts AND b.last_key_ts
ORDER BY f.ts;
"""

COUNTER_SQL = r"""
SELECT t.name, c.ts, c.value
FROM counter c JOIN track t ON c.track_id=t.id
WHERE t.name IN (
  'AuraIme runtime allocated bytes',
  'AuraIme runtime GC count',
  'AuraIme UID RX bytes',
  'AuraIme UID TX bytes',
  'AuraIme process read bytes',
  'AuraIme process write bytes',
  'AuraIme process PSS KB',
  'AuraIme ORT state',
  'AuraIme ORT provider',
  'AuraIme ORT model bytes',
  'AuraIme ORT parameter count',
  'AuraIme ORT inference count',
  'AuraIme ORT initialization us',
  'AuraIme ORT warmup us',
  'AuraIme ORT inference p50 us',
  'AuraIme ORT inference p95 us',
  'AuraIme ORT inference p99 us'
)
ORDER BY t.name, c.ts;
"""


def query(trace_processor: Path, trace: Path, sql: str) -> list[dict[str, str]]:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as file:
        file.write(sql)
        query_path = Path(file.name)
    try:
        processor_command = (
            [sys.executable, str(trace_processor)]
            if trace_processor.suffix.lower() == ".py"
            else [str(trace_processor)]
        )
        completed = subprocess.run(
            processor_command + [str(trace), "--query-file", str(query_path)],
            check=True,
            text=True,
            capture_output=True,
        )
        return list(csv.DictReader(completed.stdout.splitlines()))
    finally:
        query_path.unlink(missing_ok=True)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def has_value(value: object) -> bool:
    return value is not None and str(value).strip() not in NULL_TEXT_VALUES


def as_float(value: object, default: float = 0.0) -> float:
    return float(value) if has_value(value) else default


def as_int(value: object, default: int = 0) -> int:
    return int(as_float(value, float(default)))


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values) if values else None,
    }


def grouped_distributions(
    rows: list[dict[str, str]],
    category_key: str = "category",
    value_key: str = "duration_ms",
) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        category = row.get(category_key)
        value = row.get(value_key)
        if category and has_value(value):
            grouped.setdefault(category, []).append(as_float(value))
    return {category: distribution(values) for category, values in sorted(grouped.items())}


def counter_evidence(rows: list[dict[str, str]]) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        name = row.get("name")
        value = row.get("value")
        if name and has_value(value):
            grouped.setdefault(name, []).append(as_float(value))
    return {
        name: {
            "samples": len(values),
            "before": values[0],
            "after": values[-1],
            "delta": values[-1] - values[0],
        }
        for name, values in sorted(grouped.items())
    }


def last_label(labels: list[str], prefix: str) -> str | None:
    matching = [label[len(prefix):] for label in labels if label.startswith(prefix)]
    return matching[-1] if matching else None


def count_reordered_mutations(rows: list[dict[str, str]], character_indices: set[int]) -> int:
    last = -1
    reordered = 0
    for row in rows:
        index = int(row["event_index"])
        if index not in character_indices:
            continue
        if index < last:
            reordered += 1
        last = max(last, index)
    return reordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("trace_processor", type=Path)
    parser.add_argument("workload_result", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("baseline", "final"), default="final")
    parser.add_argument(
        "--ime-package",
        default="dev.varuntej.aura.imebenchmarktarget",
        choices=("dev.varuntej.aura.imebenchmarktarget",),
    )
    args = parser.parse_args()

    workload = json.loads(args.workload_result.read_text(encoding="utf-8-sig"))
    event_rows = query(args.trace_processor, args.trace, EVENT_LATENCY_SQL)
    mutation_rows = query(args.trace_processor, args.trace, MUTATION_ORDER_SQL)
    runtime_rows = query(
        args.trace_processor,
        args.trace,
        RUNTIME_SQL.replace("__IME_PACKAGE__", args.ime_package),
    )
    ort_label_rows = query(args.trace_processor, args.trace, ORT_LABEL_SQL)
    suggestion_rows = query(args.trace_processor, args.trace, SUGGESTION_SQL)
    inference_rows = query(args.trace_processor, args.trace, INFERENCE_SQL)
    duration_rows = query(args.trace_processor, args.trace, DURATION_SQL)
    frame_rows = query(args.trace_processor, args.trace, FRAME_SQL)
    counter_rows = query(args.trace_processor, args.trace, COUNTER_SQL)

    character_indices = {int(value) for value in workload["character_event_indices"]}
    character_rows = [row for row in event_rows if int(row["event_index"]) in character_indices]
    release_to_commit = [
        as_float(row["release_to_commit_ms"])
        for row in character_rows
        if has_value(row.get("release_to_commit_ms"))
    ]
    commit_to_present = [
        as_float(row["commit_to_present_ms"])
        for row in character_rows
        if has_value(row.get("commit_to_present_ms"))
    ]
    release_to_present = [
        as_float(row["release_to_present_ms"])
        for row in character_rows
        if has_value(row.get("release_to_present_ms"))
    ]
    missed = sum(1 for row in character_rows if as_int(row.get("mutation_count")) == 0)
    duplicated = sum(max(0, as_int(row.get("mutation_count")) - 1) for row in character_rows)
    reordered = count_reordered_mutations(mutation_rows, character_indices)
    delayed = sum(value > 8.0 for value in release_to_commit)

    runtime = runtime_rows[0] if runtime_rows else {}
    labels = [row["name"] for row in ort_label_rows if row.get("name")]
    ort = {
        "state": last_label(labels, "AuraIme:ORT state:"),
        "provider": last_label(labels, "AuraIme:ORT provider:"),
        "runtime_version": last_label(labels, "AuraIme:ORT runtime:"),
        "failure_category": last_label(labels, "AuraIme:ORT failure:"),
        "inference_slices": as_int(runtime.get("ort_inference_slices")),
        "diagnostic_inference_count": as_int(runtime.get("ort_diagnostic_inference_count")),
    }
    inference = [
        as_float(row["inference_ms"])
        for row in inference_rows
        if has_value(row.get("inference_ms"))
    ]
    suggestions = grouped_distributions(suggestion_rows, value_key="latency_ms")
    lexical_suggestions = suggestions.get("AuraIme suggestion lexical latency us", distribution([]))
    deferred_suggestions = suggestions.get("AuraIme suggestion deferred latency us", distribution([]))
    durations = grouped_distributions(duration_rows)
    frames = [
        as_float(row["frame_ms"])
        for row in frame_rows
        if has_value(row.get("frame_ms"))
    ]
    janky_frames = sum(
        1 for row in frame_rows if has_value(row.get("jank_type"))
    )
    counters = counter_evidence(counter_rows)
    counter_delta = lambda name: counters.get(name, {}).get("delta")
    expected_events = int(workload["event_count"])
    expected_characters = len(character_indices)
    onnx_ready = ort["state"] == "READY" and ort["provider"] in ("CPU", "XNNPACK")
    onnx_active = ort["inference_slices"] > 0 and ort["diagnostic_inference_count"] > 0
    explicit_lexical_fallback = ort["state"] == "FAILED" and bool(ort["failure_category"])

    gates = {
        "all_event_releases_traced": len(event_rows) == expected_events,
        "all_character_events_traced": len(character_rows) == expected_characters,
        "all_character_commits_observed": len(release_to_commit) == expected_characters,
        "all_character_commits_presented": len(release_to_present) == expected_characters,
        "zero_missed_characters": missed == 0,
        "zero_duplicated_characters": duplicated == 0,
        "zero_reordered_characters": reordered == 0,
        "sustained_output_exact": workload["dropped_duplicated_or_reordered"] is False,
        "all_core_loop_scenarios": workload["all_scenarios_passed"] is True,
    }
    if args.mode == "final":
        gates.update(
            {
                "zero_delayed_characters": delayed == 0,
                "release_to_commit_p99_at_most_8_ms": bool(release_to_commit)
                and percentile(release_to_commit, 0.99) <= 8.0,
                "release_to_present_p99_below_16_ms": bool(release_to_present)
                and percentile(release_to_present, 0.99) < 16.0,
                "suggestion_lexical_p95_at_most_70_ms": lexical_suggestions["count"] > 0
                and float(lexical_suggestions["p95_ms"]) <= 70.0,
                "suggestion_lexical_p99_at_most_100_ms": lexical_suggestions["count"] > 0
                and float(lexical_suggestions["p99_ms"]) <= 100.0,
                "suggestion_deferred_p95_at_most_220_ms": deferred_suggestions["count"] > 0
                and float(deferred_suggestions["p95_ms"]) <= 220.0,
                "onnx_ready_and_active_or_explicit_fallback":
                    (onnx_ready and onnx_active) or explicit_lexical_fallback,
                "prediction_queue_bounded":
                    as_int(runtime.get("max_prediction_pending"), 99) <= 2,
                "one_active_prediction":
                    as_int(runtime.get("max_prediction_active"), 99) <= 1,
                "runtime_counters_captured": all(
                    counters.get(name, {}).get("samples", 0) >= 2
                    for name in (
                        "AuraIme runtime allocated bytes",
                        "AuraIme runtime GC count",
                        "AuraIme UID RX bytes",
                        "AuraIme UID TX bytes",
                        "AuraIme process read bytes",
                        "AuraIme process write bytes",
                        "AuraIme process PSS KB",
                    )
                ),
                "no_process_disk_bytes":
                    counter_delta("AuraIme process read bytes") == 0
                    and counter_delta("AuraIme process write bytes") == 0,
                "no_uid_network_bytes":
                    counter_delta("AuraIme UID RX bytes") == 0
                    and counter_delta("AuraIme UID TX bytes") == 0,
                "no_app_thread_disk_events": as_int(runtime.get("traced_disk_events")) == 0,
                "no_app_thread_network_events": as_int(runtime.get("traced_network_events")) == 0,
            }
        )

    report = {
        "mode": args.mode,
        "ime_package": args.ime_package,
        "metrics": {
            "key_release_to_character_commit": distribution(release_to_commit),
            "character_commit_to_target_frame_presentation": distribution(commit_to_present),
            "key_release_to_target_frame_presentation": distribution(release_to_present),
            "suggestions": {
                "lexical_request_to_ui_apply": lexical_suggestions,
                "deferred_request_to_ui_apply": deferred_suggestions,
            },
            "onnx_inference": distribution(inference),
        },
        "correctness": {
            "expected_events": expected_events,
            "traced_release_events": len(event_rows),
            "expected_character_events": expected_characters,
            "missed_characters": missed,
            "duplicated_characters": duplicated,
            "reordered_characters": reordered,
            "delayed_characters_over_8_ms": delayed,
            "scenario_results": workload["scenarios"],
        },
        "onnx": ort,
        "runtime": runtime,
        "performance": {
            "durations": durations,
            "frame_timeline": {
                **distribution(frames),
                "janky_frame_count": janky_frames,
            },
            "runtime_counters": counters,
        },
        "workload": workload,
        "gates": gates,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    if not all(gates.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
