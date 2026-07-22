"""Preflight for the meeting-audio Cloud Storage bucket.

Two modes, one vocabulary:

  --check  Read-only. Verify the bucket named by MEETINGS_AUDIO_BUCKET (or
           --bucket) EXISTS, sits in the expected region, and carries the
           7-day DELETE lifecycle rule, and that the caller can reach it.
           Writes nothing. Non-zero exit on any failing condition. This is the
           deploy gate: deploy.sh runs it before shifting traffic so a missing
           bucket can never ship again (2026-07-14 incident: the bucket was
           never provisioned, so every upload 404'd and no meeting produced a
           note - see lessons-learnt.txt / ECOSYSTEM.md).

  --smoke  Synthetic create -> read -> list -> delete of ONE non-user object
           under meetings/__preflight__/. Confirms the pipeline's exact GCS
           operations round-trip end to end, then removes the object. Never
           touches real meetings/{uid}/... data. Use after provisioning and
           post-deploy. Do NOT use a real meeting to test infrastructure.

Run from the backend directory:

    cd backend && python scripts/check_meeting_storage.py --check \\
        [--project juno-2ea45] [--bucket juno-2ea45-meeting-audio] \\
        [--region us-central1]

    cd backend && python scripts/check_meeting_storage.py --smoke \\
        [--project juno-2ea45] [--bucket juno-2ea45-meeting-audio]

Reuses services/meetings/gcs_audio.py (client singleton + bucket_name) so this
check and the runtime upload path always agree on which bucket is authoritative.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from src.services.meetings import gcs_audio  # noqa: E402

# Stable reason strings shared with the deploy gate and (later) the runtime
# storage-health classification that maps a missing bucket to
# upload_storage_unavailable instead of an opaque 503.
REASON_OK = "ok"
REASON_ACCESS_DENIED = "access_denied"
REASON_BUCKET_MISSING = "bucket_missing"
REASON_REGION_MISMATCH = "region_mismatch"
REASON_LIFECYCLE_MISSING = "lifecycle_missing"
REASON_IAM_BINDING_MISSING = "iam_binding_missing"

DEFAULT_REGION = "us-central1"
LIFECYCLE_DELETE_AGE_DAYS = 7

# Synthetic objects live under a clearly non-user prefix so they can never
# collide with real meetings/{uid}/{meeting_id}/ captures.
PREFLIGHT_PREFIX = "meetings/__preflight__/"


@dataclass(frozen=True)
class BucketSnapshot:
    """What the check learned about the bucket from one metadata read. Kept as a
    plain value so classification is pure and unit-testable without any GCS call."""
    exists: bool
    accessible: bool
    location: str | None
    delete_ages: tuple[int, ...]


@dataclass(frozen=True)
class StorageCheck:
    ok: bool
    reason: str
    detail: str


def _extract_delete_ages(lifecycle_rules: Any) -> tuple[int, ...]:
    """Ages of every DELETE lifecycle rule. Tolerates both the dict form that
    google-cloud-storage yields and an attribute form, so a client-lib shape
    change does not silently drop the rule and fail an otherwise-healthy bucket."""
    def _field(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    ages: list[int] = []
    for rule in lifecycle_rules or []:
        action = _field(rule, "action")
        action_type = action.get("type") if isinstance(action, dict) else action
        if str(action_type).lower() != "delete":
            continue
        condition = _field(rule, "condition")
        age = condition.get("age") if isinstance(condition, dict) else _field(condition, "age")
        if age is not None:
            ages.append(int(age))
    return tuple(ages)


def classify(snapshot: BucketSnapshot, *, expected_region: str,
             want_delete_age: int) -> StorageCheck:
    """Pure mapping from a bucket snapshot to a stable reason. Order matters:
    an unreachable/missing bucket must be reported as such before region or
    lifecycle, which we cannot even read in those cases."""
    if not snapshot.accessible:
        return StorageCheck(False, REASON_ACCESS_DENIED,
                            "Cannot access the bucket (permission denied for the caller).")
    if not snapshot.exists:
        return StorageCheck(False, REASON_BUCKET_MISSING,
                            "Bucket does not exist. Provision it before deploying.")
    if snapshot.location and snapshot.location.lower() != expected_region.lower():
        return StorageCheck(False, REASON_REGION_MISMATCH,
                            f"Bucket region is {snapshot.location}, expected {expected_region}.")
    if want_delete_age not in snapshot.delete_ages:
        return StorageCheck(False, REASON_LIFECYCLE_MISSING,
                            f"No {want_delete_age}-day DELETE lifecycle rule found "
                            f"(found delete ages: {snapshot.delete_ages or 'none'}).")
    return StorageCheck(True, REASON_OK, "Bucket exists, region matches, lifecycle present.")


def classify_iam_binding(
    policy: Any,
    *,
    required_member: str,
    required_role: str,
) -> StorageCheck:
    """Verify the configured runtime principal has the required bucket role.

    The metadata check runs as the deploy operator, which proves the deploy can
    inspect the bucket but not that Cloud Run's mounted service-account key can
    use it. Reading the bucket IAM policy closes that configuration gap without
    impersonation or a write during the deploy gate.
    """
    bindings = getattr(policy, "bindings", None)
    if bindings is None and isinstance(policy, dict):
        bindings = policy.get("bindings", [])

    for binding in bindings or []:
        role = binding.get("role") if isinstance(binding, dict) else getattr(binding, "role", "")
        members = (
            binding.get("members", [])
            if isinstance(binding, dict)
            else getattr(binding, "members", [])
        )
        if role == required_role and required_member in members:
            return StorageCheck(True, REASON_OK, "Runtime service-account IAM binding present.")

    return StorageCheck(
        False,
        REASON_IAM_BINDING_MISSING,
        f"{required_member} does not have {required_role} on the bucket.",
    )


def _storage_client(project: str | None) -> Any:
    """Reuse the runtime client singleton when no project override is given, so
    the check exercises the same credentials/config the uploader will. A --project
    override (used by the deploy gate) builds a project-pinned client instead."""
    if project:
        from google.cloud import storage  # type: ignore
        return storage.Client(project=project)
    return gcs_audio._client()


def snapshot_bucket(client: Any, bucket_name: str) -> BucketSnapshot:
    """One metadata read, turned into a BucketSnapshot. lookup_bucket returns None
    for a missing bucket (no error) and raises Forbidden when the bucket exists
    but the caller lacks access - the two are different reasons."""
    from google.api_core import exceptions as gexc  # type: ignore
    try:
        bucket = client.lookup_bucket(bucket_name)
    except gexc.Forbidden:
        return BucketSnapshot(exists=False, accessible=False, location=None, delete_ages=())
    if bucket is None:
        return BucketSnapshot(exists=False, accessible=True, location=None, delete_ages=())
    return BucketSnapshot(
        exists=True,
        accessible=True,
        location=getattr(bucket, "location", None),
        delete_ages=_extract_delete_ages(getattr(bucket, "lifecycle_rules", None)),
    )


def run_check(
    *,
    project: str | None,
    bucket_name: str,
    region: str,
    required_member: str | None = None,
    required_role: str | None = None,
) -> int:
    client = _storage_client(project)
    snapshot = snapshot_bucket(client, bucket_name)
    result = classify(snapshot, expected_region=region, want_delete_age=LIFECYCLE_DELETE_AGE_DAYS)
    if result.ok and required_member and required_role:
        try:
            policy = client.bucket(bucket_name).get_iam_policy(requested_policy_version=3)
            result = classify_iam_binding(
                policy,
                required_member=required_member,
                required_role=required_role,
            )
        except Exception as exc:  # noqa: BLE001 - stable deploy-gate result
            result = StorageCheck(
                False,
                REASON_ACCESS_DENIED,
                f"Cannot verify the runtime IAM binding: {exc}",
            )
    status = "OK" if result.ok else "FAIL"
    print(f"[{status}] bucket={bucket_name} reason={result.reason}")
    print(f"       {result.detail}")
    return 0 if result.ok else 1


def run_smoke(*, project: str | None, bucket_name: str) -> int:
    client = _storage_client(project)
    run_id = uuid.uuid4().hex[:12]
    path = f"{PREFLIGHT_PREFIX}{run_id}/0000.flac"
    prefix = f"{PREFLIGHT_PREFIX}{run_id}/"
    payload = b"fLaC" + bytes(60)  # synthetic, non-user; enough to prove a round-trip
    bucket = client.bucket(bucket_name)

    try:
        print(f"create -> {path}")
        bucket.blob(path).upload_from_string(payload, content_type="audio/flac")

        print("read   -> download_as_bytes")
        got = bucket.blob(path).download_as_bytes()
        if got != payload:
            print("[FAIL] downloaded bytes did not match uploaded bytes")
            return 1

        print("list   -> list_blobs(prefix)")
        names = [blob.name for blob in client.list_blobs(bucket_name, prefix=prefix)]
        if path not in names:
            print(f"[FAIL] uploaded object not found in listing: {names}")
            return 1
    finally:
        # Never leave a synthetic object behind, even if a step above failed.
        try:
            bucket.blob(path).delete()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"[WARN] cleanup delete raised: {exc}")

    print("delete -> confirm gone")
    remaining = [blob.name for blob in client.list_blobs(bucket_name, prefix=prefix)]
    if remaining:
        print(f"[FAIL] object still present after delete: {remaining}")
        return 1

    print(f"[OK] synthetic round-trip succeeded and left nothing behind (bucket={bucket_name})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="Read-only bucket existence/region/lifecycle/access check.")
    mode.add_argument("--smoke", action="store_true",
                      help="Synthetic create/read/list/delete round-trip.")
    parser.add_argument("--project", default=None,
                        help="GCP project. Omit to use application-default credentials.")
    parser.add_argument("--bucket", default=None,
                        help="Bucket name. Omit to use MEETINGS_AUDIO_BUCKET / its default.")
    parser.add_argument("--region", default=DEFAULT_REGION,
                        help=f"Expected bucket region (default: {DEFAULT_REGION}).")
    parser.add_argument("--required-member", default=None,
                        help="Bucket IAM member required by the runtime.")
    parser.add_argument("--required-role", default=None,
                        help="Bucket IAM role required by the runtime member.")
    args = parser.parse_args()

    bucket_name = args.bucket or gcs_audio.bucket_name()

    if args.check:
        sys.exit(run_check(
            project=args.project,
            bucket_name=bucket_name,
            region=args.region,
            required_member=args.required_member,
            required_role=args.required_role,
        ))
    else:
        sys.exit(run_smoke(project=args.project, bucket_name=bucket_name))


if __name__ == "__main__":
    main()
