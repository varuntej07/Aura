from google.api_core.iam import Policy

from scripts import check_meeting_storage as storage_check


def _snapshot(
    *,
    exists: bool = True,
    accessible: bool = True,
    location: str | None = "us-central1",
    delete_ages: tuple[int, ...] = (7,),
) -> storage_check.BucketSnapshot:
    return storage_check.BucketSnapshot(
        exists=exists,
        accessible=accessible,
        location=location,
        delete_ages=delete_ages,
    )


def test_bucket_metadata_check_accepts_the_required_shape():
    result = storage_check.classify(
        _snapshot(),
        expected_region="us-central1",
        want_delete_age=7,
    )

    assert result.ok
    assert result.reason == storage_check.REASON_OK


def test_bucket_metadata_check_maps_each_deploy_blocker():
    cases = (
        (_snapshot(accessible=False), storage_check.REASON_ACCESS_DENIED),
        (_snapshot(exists=False), storage_check.REASON_BUCKET_MISSING),
        (_snapshot(location="europe-west1"), storage_check.REASON_REGION_MISMATCH),
        (_snapshot(delete_ages=()), storage_check.REASON_LIFECYCLE_MISSING),
    )

    for snapshot, expected_reason in cases:
        result = storage_check.classify(
            snapshot,
            expected_region="us-central1",
            want_delete_age=7,
        )
        assert not result.ok
        assert result.reason == expected_reason


def test_runtime_service_account_binding_is_required():
    member = "serviceAccount:runtime@example.iam.gserviceaccount.com"
    role = "roles/storage.objectAdmin"
    policy = Policy(version=3)
    policy.bindings = [{"role": role, "members": {member}}]

    present = storage_check.classify_iam_binding(
        policy,
        required_member=member,
        required_role=role,
    )
    missing = storage_check.classify_iam_binding(
        policy,
        required_member="serviceAccount:other@example.iam.gserviceaccount.com",
        required_role=role,
    )

    assert present.ok
    assert not missing.ok
    assert missing.reason == storage_check.REASON_IAM_BINDING_MISSING
