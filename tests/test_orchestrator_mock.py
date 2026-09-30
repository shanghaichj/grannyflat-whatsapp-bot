import os

os.environ["MOCK_LLM"] = "1"

from grannyflat_bot.config import get_settings
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.session.store import SessionStore


def test_orchestrator_collects_quote_requirements_without_inventing_price(tmp_path):
    get_settings.cache_clear()
    store = SessionStore(db_path=tmp_path / "s.sqlite3")
    orch = ChatOrchestrator(sessions=store)

    r1 = orch.handle("t1", "Hi, I am in NSW and want a 2 bed turnkey granny flat")
    assert r1.reply
    assert r1.questionnaire.get("state") == "NSW"
    assert r1.questionnaire.get("bedrooms") == 2
    assert r1.questionnaire.get("delivery_type") == "turnkey"

    r2 = orch.handle("t1", "Please give me a quote")
    # Production customer pricing comes from approved Twilio templates, not the
    # placeholder rule engine.  The orchestrator must never expose mock prices.
    assert r2.quote is None
    assert r2.reply
    assert r2.layout_template is not None

    session = store.get("t1")
    assert session.ai_summary
    assert session.qualification_score >= 40
    assert session.qualification_level in {"developing", "sales_ready"}
    assert session.next_action
    assert session.follow_up_due_at


def test_role_picker_waits_until_after_first_turn_and_only_when_role_unknown(tmp_path):
    orch = ChatOrchestrator(sessions=SessionStore(db_path=tmp_path / "role.sqlite3"))

    assert orch._maybe_role_template(
        contact_type="unknown", sent_template_keys=[], customer_round=1
    ) is None
    template = orch._maybe_role_template(
        contact_type="unknown", sent_template_keys=[], customer_round=2
    )
    assert template == {
        "sid": "HXad1b8c385087a29613227481a2953d15",
        "key": "role",
        "name": "role",
        "variables": None,
    }
    assert orch._maybe_role_template(
        contact_type="builder", sent_template_keys=[], customer_round=2
    ) is None
    assert orch._maybe_role_template(
        contact_type="unknown", sent_template_keys=["role"], customer_round=3
    ) is None
