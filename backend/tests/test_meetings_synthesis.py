"""Focused checks for synthesis helpers shared by the fenced V2 worker."""

from __future__ import annotations

import asyncio

from src.services.meetings import deepgram as dg
from src.services.meetings import fields as F
from src.services.meetings import synthesis


def test_one_sided_flag_survives_into_the_note():
    note = asyncio.run(synthesis._synthesize_note(
        title="Weekly sync",
        transcript="",
        language="en",
        one_sided=True,
        has_gaps=False,
    ))
    assert note["one_sided"] is True


def test_empty_transcript_short_circuits_without_llm():
    note = asyncio.run(synthesis._synthesize_note(
        title="Weekly sync",
        transcript="",
        language="en",
        one_sided=False,
        has_gaps=False,
    ))
    assert note["action_items"] == []
    assert "No speech" in note["summary"]


def test_incomplete_segment_flags_the_note_partial():
    note = asyncio.run(synthesis._synthesize_note(
        title="Weekly sync",
        transcript="",
        language="en",
        one_sided=False,
        has_gaps=True,
    ))
    assert note["partial"] is True


def test_deepgram_parser_falls_back_without_inventing_speakers():
    multichannel = dg._parse(
        {
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "Mic fallback",
                                "words": [
                                    {"word": "Mic", "start": 0.0, "end": 0.2},
                                    {"word": "fallback", "start": 0.2, "end": 0.5},
                                ],
                            }
                        ]
                    },
                    {
                        "alternatives": [
                            {
                                "transcript": "Loopback fallback",
                                "words": [{"word": "Loopback", "start": 0.0, "end": 0.3}],
                            }
                        ]
                    },
                ],
            },
        }
    )
    mono = dg._parse({"results": {"transcript": "Unattributed fallback"}})

    assert [(turn.channel, turn.text) for turn in multichannel.utterances] == [
        (dg.MIC_CHANNEL, "Mic fallback"),
        (dg.LOOPBACK_CHANNEL, "Loopback fallback"),
    ]
    assert mono.utterances == [
        dg.Utterance(
            channel=-1,
            start_s=0.0,
            end_s=0.0,
            text="Unattributed fallback",
            speaker="",
        ),
    ]
