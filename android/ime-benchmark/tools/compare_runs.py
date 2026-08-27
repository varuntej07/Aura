#!/usr/bin/env python3
"""Compare same-device Aura IME baseline and final physical evidence bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(directory: Path, name: str) -> dict:
    return json.loads((directory / name).read_text(encoding="utf-8-sig"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("final", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    baseline_metadata = load(args.baseline, "device-metadata.json")
    final_metadata = load(args.final, "device-metadata.json")
    baseline = load(args.baseline, "latency-report.json")
    final = load(args.final, "latency-report.json")
    identity_fields = (
        "serial", "model", "soc", "hardware", "abi", "android_version",
        "build_fingerprint", "ram", "refresh_rate_hz", "thermal_status", "build_type",
    )
    identity_match = {
        field: baseline_metadata.get(field) not in (None, "") and
        baseline_metadata.get(field) == final_metadata.get(field)
        for field in identity_fields
    }
    workload_match = (
        baseline["workload"]["event_count"] == final["workload"]["event_count"] and
        baseline["workload"]["inter_key_ms"] == final["workload"]["inter_key_ms"] and
        baseline["workload"]["correction_settle_ms"] ==
        final["workload"]["correction_settle_ms"] and
        baseline["workload"]["correction_samples"] ==
        final["workload"]["correction_samples"]
    )
    report = {
        "same_device_build_identity": identity_match,
        "same_workload": workload_match,
        "baseline_metrics": baseline["metrics"],
        "final_metrics": final["metrics"],
        "release_to_present_p99_delta_ms":
            final["metrics"]["key_release_to_target_frame_presentation"]["p99_ms"] -
            baseline["metrics"]["key_release_to_target_frame_presentation"]["p99_ms"],
        "apk_size_delta_bytes":
            final_metadata["benchmark_ime_apk_bytes"] -
            baseline_metadata["benchmark_ime_apk_bytes"],
        "final_gates": final["gates"],
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    if not all(identity_match.values()) or not workload_match or not all(final["gates"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
