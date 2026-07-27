from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.handlers.get_better import handle_post_get_better_activity


class _Request:
    def __init__(self, body: object) -> None:
        self._body = body

    async def json(self) -> object:
        return self._body


def _event(event_id: str = "event-001") -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "opened",
        "story_id": "weigh_a_big_decision",
        "story_version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.asyncio
async def test_activity_handler_accepts_one_idempotent_batch() -> None:
    store = AsyncMock(return_value=True)
    with (
        patch(
            "src.handlers.get_better.resolve_user_id_from_request",
            return_value="user-1",
        ),
        patch("src.handlers.get_better.store_activity_batch", store),
    ):
        response = await handle_post_get_better_activity(
            _Request({"batch_id": "batch-001", "events": [_event()]})  # type: ignore[arg-type]
        )

    assert response.status_code == 200
    assert json.loads(response.body) == {"accepted": 1, "deduplicated": False}
    batch = store.await_args.args[1]
    assert batch.batch_id == "batch-001"
    assert len(batch.events) == 1


@pytest.mark.asyncio
async def test_activity_handler_rejects_duplicate_event_ids() -> None:
    with patch(
        "src.handlers.get_better.resolve_user_id_from_request",
        return_value="user-1",
    ):
        response = await handle_post_get_better_activity(
            _Request(  # type: ignore[arg-type]
                {
                    "batch_id": "batch-001",
                    "events": [_event(), _event()],
                }
            )
        )

    assert response.status_code == 400
    assert "Invalid Get Better activity batch" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_activity_handler_requires_authentication() -> None:
    with patch(
        "src.handlers.get_better.resolve_user_id_from_request",
        return_value=None,
    ):
        response = await handle_post_get_better_activity(
            _Request({"batch_id": "batch-001", "events": [_event()]})  # type: ignore[arg-type]
        )

    assert response.status_code == 401
