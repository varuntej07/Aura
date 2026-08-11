"""Preflight the separate 180-day dictation-audio Cloud Storage bucket."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

_SERVICE_ACCOUNT = _BACKEND_DIR / "service-account.json"
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and _SERVICE_ACCOUNT.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_SERVICE_ACCOUNT)

from src.services.dictation import gcs_audio  # noqa: E402

DEFAULT_REGION = "us-central1"
DELETE_AGE_DAYS = 180
REQUIRED_PREFIX = "dictation/v1/"


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _has_lifecycle_rule(rules: Any) -> bool:
    for rule in rules or []:
        action = _field(rule, "action")
        action_type = _field(action, "type") if not isinstance(action, str) else action
        condition = _field(rule, "condition") or {}
        prefixes = _field(condition, "matchesPrefix") or _field(condition, "matches_prefix") or []
        if (
            str(action_type).lower() == "delete"
            and int(_field(condition, "age") or -1) == DELETE_AGE_DAYS
            and REQUIRED_PREFIX in prefixes
        ):
            return True
    return False


def _client(project: str | None):
    if project:
        from google.cloud import storage  # type: ignore

        return storage.Client(project=project)
    return gcs_audio._client()


def run_check(
    *,
    project: str | None,
    bucket_name: str,
    region: str,
    required_member: str | None,
    required_role: str | None,
) -> int:
    client = _client(project)
    bucket = client.lookup_bucket(bucket_name)
    if bucket is None:
        print(f"[FAIL] bucket={bucket_name} reason=bucket_missing")
        return 1
    if str(bucket.location or "").lower() != region.lower():
        print(f"[FAIL] bucket={bucket_name} reason=region_mismatch location={bucket.location}")
        return 1
    if not _has_lifecycle_rule(bucket.lifecycle_rules):
        print(
            f"[FAIL] bucket={bucket_name} reason=lifecycle_missing "
            f"want=Delete(age={DELETE_AGE_DAYS},matchesPrefix={REQUIRED_PREFIX})"
        )
        return 1
    if required_member and required_role:
        policy = client.bucket(bucket_name).get_iam_policy(requested_policy_version=3)
        bindings = getattr(policy, "bindings", None) or {}
        if isinstance(bindings, dict):
            members = bindings.get(required_role, set())
        else:
            members = set()
            for binding in bindings:
                role = _field(binding, "role")
                if role == required_role:
                    members.update(_field(binding, "members") or [])
        if required_member not in members:
            print(f"[FAIL] bucket={bucket_name} reason=iam_binding_missing")
            return 1
    print(f"[OK] bucket={bucket_name} region={region} lifecycle=180-day-prefix-delete")
    return 0


async def run_smoke(bucket_name: str) -> int:
    uid = "__preflight__"
    trace_id = "0" * 24
    payload = b"fLaC" + bytes(60)
    digest = hashlib.sha256(payload).hexdigest()
    os.environ["DICTATION_AUDIO_BUCKET"] = bucket_name
    created = await gcs_audio.create_audio(uid, trace_id, digest, payload)
    replay = await gcs_audio.create_audio(uid, trace_id, digest, payload)
    downloaded = await gcs_audio.download_exact(created.path, created.generation)
    if downloaded != payload or replay.generation != created.generation or not replay.reconciled:
        print("[FAIL] create/replay/read did not preserve immutable identity")
        return 1
    await gcs_audio.delete_exact(created.path, created.generation)
    if await gcs_audio.object_exists(created.path, created.generation):
        print("[FAIL] smoke object remains after delete")
        return 1
    print(f"[OK] immutable create/replay/read/delete succeeded bucket={bucket_name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--smoke", action="store_true")
    parser.add_argument("--project", default=None)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--required-member", default=None)
    parser.add_argument("--required-role", default=None)
    args = parser.parse_args()
    name = args.bucket or gcs_audio.bucket_name()
    if args.check:
        raise SystemExit(
            run_check(
                project=args.project,
                bucket_name=name,
                region=args.region,
                required_member=args.required_member,
                required_role=args.required_role,
            )
        )
    raise SystemExit(asyncio.run(run_smoke(name)))


if __name__ == "__main__":
    main()
