#!/usr/bin/env python3
"""Summarize actual ONNX Runtime node-provider assignments from an ORT JSON profile."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    events = json.loads(args.profile.read_text(encoding="utf-8"))
    providers: Counter[str] = Counter()
    operators: Counter[str] = Counter()
    for event in events:
        details = event.get("args") or {}
        provider = details.get("provider")
        operator = details.get("op_name")
        if provider:
            providers[str(provider)] += 1
        if operator:
            operators[str(operator)] += 1
    report = {
        "profile": str(args.profile),
        "node_provider_assignments": dict(sorted(providers.items())),
        "operator_executions": dict(sorted(operators.items())),
        "provider_assignment_proven": bool(providers),
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    if not providers:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
