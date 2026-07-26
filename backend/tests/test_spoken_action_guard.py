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


def test_ordinary_and_outbound_turns_do_not_trigger():
    # Conversation and outbound-message drafting must NOT be backstopped: those
    # either stay spoken or belong to draft_outbound_message.
    for text in (
        "what is the weather today",
        "can you tell me a joke",
        "draft a reply to this email",
        "reply to Sarah on WhatsApp",
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
