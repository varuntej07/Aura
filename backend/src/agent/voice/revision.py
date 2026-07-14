"""Deterministic, secret-free identity for the exact shipped voice worker source."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from .action_policy import ACTION_POLICY_VERSION
from .context_compaction import CONTEXT_COMPACTOR_VERSION


@lru_cache(maxsize=1)
def worker_revision_fields() -> dict[str, str]:
    """Fingerprint packaged worker code without requiring Git or a new env variable."""
    agent_root = Path(__file__).resolve().parents[1]
    source_files = sorted(agent_root.rglob("*.py"))
    digest = hashlib.sha256()
    newest_mtime = 0.0
    for path in source_files:
        relative = path.relative_to(agent_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        newest_mtime = max(newest_mtime, path.stat().st_mtime)
    build_time_file = Path("/app/.voice_build_time")
    build_time = (
        build_time_file.read_text(encoding="utf-8").strip()
        if build_time_file.exists()
        else datetime.fromtimestamp(newest_mtime, tz=UTC).isoformat()
    )
    return {
        "worker_build_sha": digest.hexdigest(),
        "worker_build_time": build_time,
        "action_policy_version": ACTION_POLICY_VERSION,
        "context_compactor_version": CONTEXT_COMPACTOR_VERSION,
    }
