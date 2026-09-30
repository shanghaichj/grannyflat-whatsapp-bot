from grannyflat_bot.app import _reply_delay_seconds
from grannyflat_bot.orchestrator import _compact_customer_reply


def test_reply_delay_feels_typed_and_grows_with_length() -> None:
    short = _reply_delay_seconds("Thanks — what state is the project in?")
    medium = _reply_delay_seconds("A" * 80)
    long = _reply_delay_seconds("A" * 240)

    assert 8.0 <= short < medium < long <= 25.0


def test_reply_policy_removes_recent_duplicate_sentences() -> None:
    history = [
        {
            "role": "assistant",
            "content": "Our modules are prefabricated in China. What state is your project in?",
        }
    ]
    reply = _compact_customer_reply(
        "Our modules are prefabricated in China. We use licensed local builders. "
        "What suburb is the site in?",
        history=history,
    )

    assert "prefabricated in China" not in reply
    assert "licensed local builders" in reply
    assert reply.count("?") == 1


def test_role_picker_turn_suppresses_competing_text_question() -> None:
    reply = _compact_customer_reply(
        "Yes, we can help with that. Which Australian state is your project in?",
        history=[],
        suppress_questions=True,
    )

    assert reply == "Yes, we can help with that."
