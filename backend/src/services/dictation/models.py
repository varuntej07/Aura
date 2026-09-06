"""Strict JSON contract for finalized, consented dictation traces."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from . import fields as F


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    edit_class: Literal["verbatim", "casing", "punctuation", "disfluency", "style"] = (
        Field(alias="class")
    )
    from_text: str = Field(alias="from", max_length=4_096)
    to_text: str = Field(alias="to", max_length=4_096)
    word_index: int = Field(alias="wordIndex", ge=0, le=100_000)


class TracePayloadBase(BaseModel):
    """The shipped Aura Desktop metadata body.

    ``traceId`` is present in the client even though the path already carries
    it. The handler requires equality and then normalizes it into the
    fingerprint, so clients that omit the redundant body field remain
    idempotent with clients that include it.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    trace_id: str | None = Field(default=None, alias="traceId")
    recorded_at_ms: int = Field(alias="recordedAtMs", ge=0)
    duration_ms: int = Field(alias="durationMs", gt=0, le=F.MAX_DURATION_MS)
    audio_sha256: str = Field(
        alias="audioSha256",
        pattern=r"^[0-9a-f]{64}$",
    )
    edits: list[EditPayload] = Field(max_length=F.MAX_EDITS)

    def normalized_dict(self, trace_id: str) -> dict:
        value = self.model_dump(by_alias=True, mode="json")
        value["traceId"] = trace_id
        return value

    def fingerprint(self, trace_id: str) -> str:
        canonical = json.dumps(
            self.normalized_dict(trace_id),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class TracePayloadV2(TracePayloadBase):
    schema_version: Literal[F.TRACE_SCHEMA_VERSION] = Field(alias="schemaVersion")
    sample_rate_hz: Literal[16_000] = Field(alias="sampleRateHz")
    channels: Literal[1]
    language: Literal["en-US"]
    provider: Literal["deepgram"]
    provider_model: str = Field(alias="providerModel", min_length=1, max_length=256)
    raw_transcript: str = Field(alias="rawTranscript", min_length=1, max_length=F.MAX_TEXT_CHARS)
    inserted_text: str = Field(alias="insertedText", min_length=1, max_length=F.MAX_TEXT_CHARS)
    final_text: str = Field(alias="finalText", min_length=1, max_length=F.MAX_TEXT_CHARS)
    training_text: str = Field(alias="trainingText", min_length=1, max_length=F.MAX_TEXT_CHARS)
    label_source: Literal["observed_field"] = Field(alias="labelSource")
    label_quality: Literal[F.CLIENT_LABEL_QUALITIES] = Field(alias="labelQuality")
    normalization_version: Literal[1] = Field(alias="normalizationVersion")
    consent_version: Literal[F.CONSENT_VERSION] = Field(alias="consentVersion")


# One schema. V1 was never sent by any client and no V1 document was ever
# stored, so the union only existed to be tolerated.
TracePayload: TypeAlias = TracePayloadV2
