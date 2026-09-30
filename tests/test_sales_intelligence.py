from datetime import datetime, timezone
from pathlib import Path

from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.sales_intelligence import (
    LEAD_TEMPERATURE_LABELS,
    deterministic_summary,
    generate_ai_summary,
    qualify_lead,
)
from grannyflat_bot.session.store import Session, SessionStore


def test_customer_priority_labels_use_sales_language() -> None:
    assert LEAD_TEMPERATURE_LABELS == {
        "cold": "咨询客户",
        "warm": "意向客户",
        "hot": "重点客户",
        "non_target": "非目标客户",
    }


def test_builder_investor_and_multi_unit_are_key_customers() -> None:
    builder = qualify_lead(
        RequirementState(), history=[], sales_stage="new", contact_type="builder"
    )
    investor = qualify_lead(
        RequirementState(usage_type="investment"), history=[], sales_stage="new"
    )
    multi = qualify_lead(
        RequirementState(unit_count=2), history=[], sales_stage="new"
    )

    assert (builder.score, builder.temperature) == (90, "hot")
    assert (investor.score, investor.temperature) == (80, "hot")
    assert (multi.score, multi.temperature) == (90, "hot")


def test_port_city_bonus_and_remote_postcode_classification() -> None:
    city = qualify_lead(
        RequirementState(postcode="2000"), history=[], sales_stage="new"
    )
    remote = qualify_lead(
        RequirementState(postcode="2880"), history=[], sales_stage="new"
    )

    assert city.score == 25
    assert city.temperature == "cold"
    assert any("港口大城市" in reason for reason in city.reasons)
    assert remote.temperature == "non_target"


def test_quote_follow_up_is_intent_but_silence_remains_inquiry() -> None:
    quoted = [{"role": "assistant", "content": "WhatsApp template sent: 60price"}]
    silent = qualify_lead(
        RequirementState(), history=quoted, sales_stage="new", sent_template_keys=["60price"]
    )
    interested = qualify_lead(
        RequirementState(),
        history=quoted + [{"role": "user", "content": "What does installation include?"}],
        sales_stage="new",
        sent_template_keys=["60price"],
    )

    assert silent.temperature == "cold"
    assert interested.temperature == "warm"


def test_qualification_is_explainable_and_complete() -> None:
    requirements = RequirementState.model_validate(
        {
            "state": "NSW",
            "suburb_or_lga": "Parramatta",
            "bedrooms": 2,
            "lot_size_m2": 550,
            "budget_aud_max": 200000,
            "timeline": "within 12 months",
            "delivery_type": "turnkey",
            "usage_type": "rental",
            "contact_name": "Alex",
        }
    )
    result = qualify_lead(
        requirements,
        history=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Quote please"},
        ],
        sales_stage="qualified",
        whatsapp_phone="+61400000000",
    )

    assert result.score == 100
    assert result.level == "sales_ready"
    assert result.temperature == "hot"
    assert result.missing == []


def test_grounded_summary_uses_only_available_customer_facts() -> None:
    requirements = RequirementState(state="QLD", bedrooms=2)
    summary = deterministic_summary(
        requirements,
        history=[{"role": "user", "content": "I want a two bedroom option"}],
        sales_stage="qualified",
    )

    assert "QLD" in summary
    assert "2卧" in summary
    assert "预算" not in summary
    assert "计划时间" not in summary


def test_llm_summary_failure_falls_back_to_grounded_summary() -> None:
    class BrokenLlm:
        def chat_json(self, messages):
            raise RuntimeError("offline")

    summary, source = generate_ai_summary(
        BrokenLlm(),
        requirements=RequirementState(state="VIC", bedrooms=2),
        history=[],
        sales_stage="qualified",
    )

    assert "VIC" in summary
    assert "2卧" in summary
    assert source == "rules"  # LLM 失败必须如实标注来源，不能假装 GPT


def test_conversation_insights_respect_manual_locks() -> None:
    orchestrator = object.__new__(ChatOrchestrator)
    session = Session(session_id="wa-+61400000010")
    session.next_action = "Keep my action"
    session.next_action_manual = True
    session.follow_up_due_at = "2030-01-01T00:00:00+00:00"
    session.follow_up_due_manual = True

    orchestrator._apply_sales_intelligence(
        session,
        {
            "crm_summary": "客户正在比较两房方案。",
            "next_action": "Overwrite action",
            "follow_up_in_hours": 1,
        },
    )

    assert session.ai_summary == "客户正在比较两房方案。"
    assert session.ai_summary_source == "llm"
    assert session.next_action == "Keep my action"
    assert session.follow_up_due_at == "2030-01-01T00:00:00+00:00"


def test_conversation_insights_create_action_and_reminder() -> None:
    orchestrator = object.__new__(ChatOrchestrator)
    session = Session(session_id="wa-+61400000011", sales_stage="qualified")

    orchestrator._apply_sales_intelligence(
        session,
        {"next_action": "确认土地面积", "follow_up_in_hours": 48},
    )

    assert session.next_action == "确认土地面积"
    assert session.follow_up_due_at is not None
    assert session.ai_summary
    assert session.ai_summary_source == "rules"
    due = datetime.fromisoformat(session.follow_up_due_at)
    assert due > datetime.now(timezone.utc)


def test_sales_intelligence_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite3"
    store = SessionStore(db_path)
    session = store.get("wa-+61400000012")
    session.ai_summary = "内部摘要"
    session.ai_summary_source = "llm"
    session.ai_summary_updated_at = "2026-09-26T00:00:00+00:00"
    session.qualification_score = 65
    session.qualification_level = "developing"
    session.qualification_missing = ["预算"]
    session.lead_temperature = "warm"
    session.lead_temperature_manual = True
    session.follow_up_due_manual = True
    store.save(session)

    restored = SessionStore(db_path).get(session.session_id)

    assert restored.ai_summary == "内部摘要"
    assert restored.ai_summary_source == "llm"
    assert restored.qualification_score == 65
    assert restored.qualification_missing == ["预算"]
    assert restored.lead_temperature == "warm"
    assert restored.lead_temperature_manual is True
    assert restored.follow_up_due_manual is True
