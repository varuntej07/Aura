from src.services.action_intent_policy import (
    blocked_write_reasons_for_text_turn,
    explicitly_requests_reminder_create,
    has_unreceipted_reminder_success_claim,
    reminder_receipt_guard_armed,
)


def test_status_and_complaint_turns_cannot_create_reminders():
    # The refusal moved from the tool list to execution: the tool is exposed, the CALL
    # is blocked. Hiding it made Buddy tell users it had no reminder tool.
    for text in (
        "did the reminder set?",
        "why didn't you set it?",
        "what happened to my reminder?",
    ):
        assert blocked_write_reasons_for_text_turn(text)["set_reminder"] == (
            "status_question"
        )
        assert not explicitly_requests_reminder_create(text)


def test_new_current_turn_reminder_commands_are_authorized():
    for text in (
        "Remind me tomorrow at 5 to call Mom",
        "Please set a reminder for 5 pm",
        "Could you remind me at noon?",
    ):
        assert blocked_write_reasons_for_text_turn(text) == {}
        assert explicitly_requests_reminder_create(text)


def test_continuation_turns_are_authorized():
    # The case the old wording gate silently broke: answering Buddy's own reminder
    # question does not restate the command, so it matched nothing and the write
    # could not happen.
    for text in ("yeah 7am works", "make it 8 instead", "plan my day for me"):
        assert blocked_write_reasons_for_text_turn(text) == {}


def test_negated_reminder_is_not_a_write_request():
    assert blocked_write_reasons_for_text_turn("Don't remind me about that")[
        "set_reminder"
    ] == "negated_request"


def test_receipt_guard_arms_on_a_reply_that_answers_buddys_question():
    assert reminder_receipt_guard_armed(
        "yeah 7am works", "What time should I set that reminder for?"
    )
    assert not reminder_receipt_guard_armed("what's the weather", "It's sunny out.")


def test_unreceipted_success_claim_detection_ignores_clarifying_questions():
    assert has_unreceipted_reminder_success_claim("Your reminder is all set.")
    assert has_unreceipted_reminder_success_claim("All set, I locked that in.")
    assert not has_unreceipted_reminder_success_claim(
        "Which day should I set that reminder for?"
    )
