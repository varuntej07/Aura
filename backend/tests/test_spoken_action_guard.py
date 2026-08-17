from src.agent.voice.spoken_action_guard import (
    is_question_to_user,
    looks_copyable,
)

# The request-intent cases that used to live here (test_copyable_requests_are_detected,
# test_frustration_corrections_are_detected, test_ordinary_turns_do_not_trigger,
# test_artifact_kind_prefers_the_explicit_noun) tested wants_copyable_artifact and
# artifact_kind_for, which are gone: nothing in the voice path arms a card or names one
# from the user's wording any more. Opening a card is the model's call via the
# present_visible_artifact tool, and an open ArtifactSession arms every turn after that.
# What remains testable here is output shape, which reads Buddy's own reply.


def test_looks_copyable_gates_out_short_confirmations():
    assert not looks_copyable("sure, one sec")
    assert not looks_copyable("")
    assert looks_copyable("x" * 130)
    assert looks_copyable("line one\nline two")
    assert looks_copyable("```\ncode\n```")


def test_is_question_to_user_separates_asking_from_delivering():
    assert is_question_to_user("what tone do you want for this?")
    assert not is_question_to_user("Here is the draft.")
    # A long body that merely finishes on a question is still a body.
    assert not is_question_to_user("x" * 300 + "?")
    assert not is_question_to_user("para one\n\npara two, are you open to it?")
