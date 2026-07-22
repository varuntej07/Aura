from src.services.action_intent_policy import (
    excluded_tools_for_text_turn,
    explicitly_requests_reminder_create,
    has_unreceipted_reminder_success_claim,
)


def test_status_and_complaint_turns_cannot_create_reminders():
    for text in (
        "did the reminder set?",
        "why didn't you set it?",
        "what happened to my reminder?",
    ):
        assert "set_reminder" in excluded_tools_for_text_turn(text)
        assert not explicitly_requests_reminder_create(text)


def test_new_current_turn_reminder_commands_are_authorized():
    for text in (
        "Remind me tomorrow at 5 to call Mom",
        "Please set a reminder for 5 pm",
        "Could you remind me at noon?",
    ):
        assert excluded_tools_for_text_turn(text) == frozenset()
        assert explicitly_requests_reminder_create(text)


def test_negated_reminder_is_not_a_write_request():
    assert "set_reminder" in excluded_tools_for_text_turn("Don't remind me about that")


def test_unreceipted_success_claim_detection_ignores_clarifying_questions():
    assert has_unreceipted_reminder_success_claim("Your reminder is all set.")
    assert has_unreceipted_reminder_success_claim("All set, I locked that in.")
    assert not has_unreceipted_reminder_success_claim(
        "Which day should I set that reminder for?"
    )
