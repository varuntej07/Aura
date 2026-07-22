from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from src.services import model_provider as module


class Result(BaseModel):
    value: int


async def test_structured_output_regenerates_once_after_invalid_json(monkeypatch):
    provider = module.ModelProvider()
    call = AsyncMock(side_effect=["not json", '{"value": 7}'])
    monkeypatch.setattr(provider, "_call_gemini", call)

    result = await provider._call(
        model_id="gemini-test",
        prompt="return a number",
        system=None,
        response_model=Result,
        temperature=0,
    )

    assert result == Result(value=7)
    assert call.await_count == 2
    assert "previous response violated" in call.await_args_list[1].kwargs["prompt"]


def test_parse_failure_logs_metadata_but_never_generated_text(monkeypatch):
    provider = module.ModelProvider()
    logged: list[dict] = []
    monkeypatch.setattr(
        module.logger,
        "error",
        lambda message, metadata: logged.append(metadata),
    )
    personal = "Welcome back, Varun, private personal fact"

    with pytest.raises(ValueError, match="response did not match Result") as raised:
        provider._parse_response(personal, Result)

    assert personal not in str(raised.value)
    assert logged and "raw_preview" not in logged[0]
    assert logged[0]["response_length"] == len(personal)
    assert len(logged[0]["response_digest"]) == 12
