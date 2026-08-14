"""
Tests for src/services/notification_service.py

Covers: send_notification (all branches), NotificationResult.delivered
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from firebase_admin import exceptions, messaging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_batch_response(successes: list[bool], error_codes: list[str | None] = None):
    """Build a fake messaging.BatchResponse."""
    if error_codes is None:
        error_codes = [None] * len(successes)

    responses = []
    for ok, code in zip(successes, error_codes):
        resp = MagicMock()
        resp.success = ok
        if ok:
            resp.exception = None
        else:
            exc = MagicMock()
            exc.code = code or ""
            exc.cause = None
            resp.exception = exc
        responses.append(resp)

    batch = MagicMock(spec=messaging.BatchResponse)
    batch.responses = responses
    batch.success_count = sum(successes)
    batch.failure_count = len(successes) - sum(successes)
    return batch


def _token_doc(token: str) -> dict:
    return {"token": token, "platform": "android", "registered_at": "2026-01-01T00:00:00+00:00"}


def _batch_with_exceptions(exceptions_: list[BaseException | None]):
    responses = []
    for exc in exceptions_:
        response = MagicMock()
        response.success = exc is None
        response.exception = exc
        responses.append(response)
    batch = MagicMock(spec=messaging.BatchResponse)
    batch.responses = responses
    batch.success_count = sum(exc is None for exc in exceptions_)
    batch.failure_count = len(exceptions_) - batch.success_count
    return batch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotificationResult:
    def test_fcm_acceptance_is_not_device_receipt(self):
        from src.services.notification_service import NotificationResult
        r = NotificationResult(tokens_targeted=1, success_count=1, failure_count=0)
        assert r.accepted is True
        assert r.delivered is False

    def test_delivered_false_when_success_count_zero(self):
        from src.services.notification_service import NotificationResult
        r = NotificationResult(tokens_targeted=1, success_count=0, failure_count=1)
        assert r.delivered is False

    def test_delivered_false_when_no_tokens_targeted(self):
        from src.services.notification_service import NotificationResult
        r = NotificationResult(tokens_targeted=0, success_count=0, failure_count=0)
        assert r.delivered is False


class TestSendNotification:
    @pytest.mark.asyncio
    async def test_no_tokens_returns_zero_result(self):
        from src.services.notification_service import send_notification

        with patch("src.services.notification_service.get_user_tokens", return_value=[]):
            result = await send_notification("user1", title="T", body="B")

        assert result.tokens_targeted == 0
        assert result.success_count == 0
        assert result.failure_count == 0
        assert result.delivered is False

    @pytest.mark.asyncio
    async def test_all_tokens_succeed(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok_a"), _token_doc("tok_b")]
        batch = _make_batch_response([True, True])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                result = await send_notification("user1", title="T", body="B")

        assert result.tokens_targeted == 2
        assert result.success_count == 2
        assert result.failure_count == 0
        assert result.accepted is True
        assert result.delivered is False
        assert result.invalid_tokens == []

    @pytest.mark.asyncio
    async def test_invalid_token_registration_not_registered_is_auto_deleted(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("bad_token")]
        batch = _make_batch_response([False], ["registration-token-not-registered"])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
                    result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == ["bad_token"]
        mock_remove.assert_called_once_with(
            "user1", ["bad_token"], reason="registration-token-not-registered"
        )

    @pytest.mark.asyncio
    async def test_generic_invalid_argument_does_not_delete_token(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("bad_token")]
        batch = _make_batch_response([False], ["invalid-argument"])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
                    result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == []
        mock_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_top_level_payload_invalid_argument_never_prunes(self):
        from src.services.notification_service import send_notification

        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast.side_effect = exceptions.InvalidArgumentError(
            "invalid APNs payload"
        )
        with patch(
            "src.services.notification_service.get_user_tokens",
            return_value=[_token_doc("valid-token")],
        ), patch(
            "src.services.notification_service.admin_messaging", return_value=mock_msg
        ), patch(
            "src.services.notification_service.remove_invalid_tokens"
        ) as mock_remove, patch(
            "src.services.notification_service.notification_ledger.record_send"
        ) as record_send:
            result = await send_notification("user1", title="T", body="B")

        assert result.failure_count == 1
        assert result.invalid_tokens == []
        mock_remove.assert_not_called()
        assert record_send.await_args.kwargs["accepted"] is False

    @pytest.mark.asyncio
    async def test_locally_invalid_payload_is_failed_before_fcm_without_pruning(self):
        from src.services.notification_service import send_notification

        mock_msg = MagicMock()
        with patch(
            "src.services.notification_service.get_user_tokens",
            return_value=[_token_doc("valid-token")],
        ), patch(
            "src.services.notification_service.admin_messaging", return_value=mock_msg
        ), patch(
            "src.services.notification_service.remove_invalid_tokens"
        ) as mock_remove, patch(
            "src.services.notification_service.notification_ledger.record_send"
        ) as record_send:
            result = await send_notification(
                "user1", title="T", body="B", data={"invalid": 7}  # type: ignore[dict-item]
            )

        assert result.failure_count == 1
        mock_msg.send_each_for_multicast.assert_not_called()
        mock_remove.assert_not_called()
        assert record_send.await_args.kwargs["accepted"] is False

    @pytest.mark.asyncio
    async def test_malformed_token_with_validated_payload_is_deleted(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("malformed")]
        batch = _batch_with_exceptions([
            exceptions.InvalidArgumentError("invalid registration token")
        ])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast.return_value = batch

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens), \
             patch("src.services.notification_service.admin_messaging", return_value=mock_msg), \
             patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
            result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == ["malformed"]
        mock_remove.assert_called_once_with(
            "user1", ["malformed"], reason="malformed_token_validated_payload"
        )

    @pytest.mark.asyncio
    async def test_sender_id_mismatch_is_deleted(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("wrong-sender")]
        batch = _batch_with_exceptions([
            messaging.SenderIdMismatchError("sender mismatch")
        ])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast.return_value = batch

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens), \
             patch("src.services.notification_service.admin_messaging", return_value=mock_msg), \
             patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
            result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == ["wrong-sender"]
        mock_remove.assert_called_once_with(
            "user1", ["wrong-sender"], reason="sender_id_mismatch"
        )

    @pytest.mark.asyncio
    async def test_mixed_multicast_prunes_only_unregistered_token(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("valid"), _token_doc("expired")]
        batch = _batch_with_exceptions([
            None, messaging.UnregisteredError("unregistered")
        ])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast.return_value = batch

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens), \
             patch("src.services.notification_service.admin_messaging", return_value=mock_msg), \
             patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
            result = await send_notification("user1", title="T", body="B")

        assert result.success_count == 1
        assert result.invalid_tokens == ["expired"]
        mock_remove.assert_called_once_with("user1", ["expired"], reason="unregistered")

    @pytest.mark.asyncio
    async def test_unknown_error_code_not_deleted(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok_x")]
        batch = _make_batch_response([False], ["some-transient-error"])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
                    result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == []
        mock_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_collapse_key_goes_into_apns_headers(self):
        """collapse_key must appear in APNS apns-collapse-id header."""
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok")]
        batch = _make_batch_response([True])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        captured: list = []

        original_multicast = messaging.MulticastMessage

        def capture_message(**kwargs):
            msg = original_multicast(**kwargs)
            captured.append(msg)
            return msg

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.messaging.MulticastMessage", side_effect=capture_message):
                    await send_notification(
                        "user1", title="T", body="B", collapse_key="reminder_abc"
                    )

        assert captured[0].android.collapse_key == "reminder_abc"
        assert captured[0].apns.headers["apns-collapse-id"] == "reminder_abc"
        mock_msg.send_each_for_multicast.assert_called_once()

    @pytest.mark.parametrize("identity", ["x" * 500, "🧬" * 200])
    def test_overlong_collapse_identity_is_platform_valid(self, identity):
        from src.services.notification_service import canonical_collapse_key

        result = canonical_collapse_key(identity)

        assert len(result.encode("utf-8")) <= 64
        assert result == canonical_collapse_key(identity)
        assert result.isascii()

    def test_distinct_long_collapse_identities_do_not_collide(self):
        from src.services.notification_service import canonical_collapse_key

        assert canonical_collapse_key("thread:" + "a" * 500) != canonical_collapse_key(
            "thread:" + "b" * 500
        )

    @pytest.mark.asyncio
    async def test_android_recipient_accepts_valid_apns_configuration(self):
        from src.services.notification_service import send_notification

        batch = _make_batch_response([True])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast.return_value = batch
        captured = []
        original_multicast = messaging.MulticastMessage

        def capture_message(**kwargs):
            message = original_multicast(**kwargs)
            captured.append(message)
            return message

        with patch(
            "src.services.notification_service.get_user_tokens",
            return_value=[_token_doc("android-token")],
        ), patch(
            "src.services.notification_service.admin_messaging", return_value=mock_msg
        ), patch(
            "src.services.notification_service.messaging.MulticastMessage",
            side_effect=capture_message,
        ):
            result = await send_notification(
                "user1", title="T", body="B", collapse_key="🧬" * 200
            )

        assert result.accepted is True
        collapse_id = captured[0].apns.headers["apns-collapse-id"]
        assert collapse_id == captured[0].android.collapse_key
        assert len(collapse_id.encode("utf-8")) <= 64

    @pytest.mark.asyncio
    async def test_data_dict_merged_into_payload(self):
        """data kwarg must be merged into FCM payload (line 137: payload.update(data))."""
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok")]
        batch = _make_batch_response([True])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                result = await send_notification(
                    "user1", title="T", body="B", data={"reminder_id": "abc123"}
                )

        assert result.success_count == 1

    @pytest.mark.asyncio
    async def test_data_none_does_not_crash(self):
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok")]
        batch = _make_batch_response([True])
        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                result = await send_notification("user1", title="T", body="B", data=None)

        assert result.success_count == 1

    @pytest.mark.asyncio
    async def test_unregistered_error_instance_is_auto_deleted(self):
        """Regression: FCM raises a real messaging.UnregisteredError (canonical
        code NOT_FOUND, "Requested entity was not found.") for stale tokens. The
        old code matched only hyphenated string codes and normalised exc.code to
        "not_found", which was absent from the prune set, so dead tokens were
        never deleted and retried on every tick. Detection is now by exception
        type, which must catch this."""
        from src.services.notification_service import send_notification

        tokens = [_token_doc("dead_token")]

        resp = MagicMock()
        resp.success = False
        resp.exception = messaging.UnregisteredError("Requested entity was not found.")

        batch = MagicMock(spec=messaging.BatchResponse)
        batch.responses = [resp]
        batch.success_count = 0
        batch.failure_count = 1

        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
                    result = await send_notification("user1", title="T", body="B")

        assert result.invalid_tokens == ["dead_token"]
        mock_remove.assert_called_once_with("user1", ["dead_token"], reason="unregistered")

    @pytest.mark.asyncio
    async def test_error_code_extracted_from_exc_cause(self):
        """Error code can be on exc.cause.error_code (nested firebase exception)."""
        from src.services.notification_service import send_notification

        tokens = [_token_doc("tok")]
        batch = _make_batch_response([False])
        # Simulate nested error: exc.code is empty, exc.cause.error_code has the code
        resp = batch.responses[0]
        resp.exception.code = ""
        cause = MagicMock()
        cause.error_code = "messaging/registration-token-not-registered"
        resp.exception.cause = cause

        mock_msg = MagicMock()
        mock_msg.send_each_for_multicast = MagicMock(return_value=batch)

        with patch("src.services.notification_service.get_user_tokens", return_value=tokens):
            with patch("src.services.notification_service.admin_messaging", return_value=mock_msg):
                with patch("src.services.notification_service.remove_invalid_tokens") as mock_remove:
                    result = await send_notification("user1", title="T", body="B")

        # The error_code is split on "/" and lowercased → "registration-token-not-registered"
        assert "tok" in result.invalid_tokens
        mock_remove.assert_called_once()
