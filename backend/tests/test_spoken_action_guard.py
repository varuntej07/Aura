from src.agent.voice.spoken_action_guard import (
    artifact_kind_for,
    looks_copyable,
    wants_copyable_artifact,
)


def test_copyable_requests_are_detected():
    for text in (
        "Draft me a prompt for Claude Code to review my backend",
        "draft me a prompt",
        "give me a command to restart the service",
        "write the code for a debounce function",
        "make me a prompt for Claude Code",
    ):
        assert wants_copyable_artifact(text), text


def test_frustration_corrections_are_detected():
    for text in (
        "why are you spitting it out",
        "I asked you to give me a prompt",
        "don't read it out loud, put it on screen",
        "stop reading it out",
    ):
        assert wants_copyable_artifact(text), text


def test_ordinary_turns_do_not_trigger():
    # Conversation must never be backstopped. Outbound drafting used to be
    # excluded here too, on the grounds that it belongs to
    # draft_outbound_message. A live session showed the failure path that
    # assumption ignores: when that tool does not fire, the draft is read aloud,
    # which is the exact bug this module exists to stop. The backstop only runs
    # when NO tool fired, so a working outbound draft is still untouched; all
    # that changed is that a narrated one now lands on a card instead of in the
    # user's ear, at the cost of being ephemeral rather than refinable.
    for text in (
        "what is the weather today",
        "can you tell me a joke",
        "",
    ):
        assert not wants_copyable_artifact(text), text


def test_looks_copyable_gates_out_short_confirmations():
    assert not looks_copyable("sure, one sec")
    assert not looks_copyable("")
    assert looks_copyable("x" * 130)
    assert looks_copyable("line one\nline two")
    assert looks_copyable("```\ncode\n```")


def test_artifact_kind_prefers_the_explicit_noun():
    # "prompt" wins even though "Code" is in the product name.
    assert artifact_kind_for("draft me a prompt for Claude Code") == ("prompt", "Prompt")
    assert artifact_kind_for("give me a powershell command") == ("command", "Command")
    assert artifact_kind_for("write me a regex") == ("code", "Snippet")
