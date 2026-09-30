from pathlib import Path

from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.session.store import Session, SessionStore


def test_sales_stage_advances_conservatively() -> None:
    session = Session(session_id="wa-+61400000001")
    session.sync_contact_fields()
    assert session.sales_stage == "new_lead"
    assert session.next_action == "首次联系客户"

    session.add_message("assistant", "Hello")
    session.sync_contact_fields()
    assert session.sales_stage == "contacted"

    session.requirements = session.requirements.apply_patch(
        {"state": "NSW", "bedrooms": 2}, source="test"
    )
    session.sync_contact_fields()
    assert session.sales_stage == "qualified"

    session.requirements = session.requirements.apply_patch(
        {"approx_floor_area_m2": 60}, source="test"
    )
    session.sync_contact_fields()
    assert session.sales_stage == "design_discussion"

    session.sent_template_keys.append("60price")
    session.sync_contact_fields()
    assert session.sales_stage == "quotation"
    assert [item["to"] for item in session.sales_stage_history] == [
        "contacted",
        "qualified",
        "design_discussion",
        "quotation",
    ]


def test_manual_sales_stage_lock_prevents_automatic_override() -> None:
    session = Session(session_id="wa-+61400000002")
    session.set_sales_stage(
        "site_assessment", source="agent", note="Booked visit", manual=True
    )
    session.last_quote = {"total": 123}
    session.sent_template_keys.append("60price")

    session.sync_contact_fields()

    assert session.sales_stage == "site_assessment"
    assert session.sales_stage_manual is True
    assert session.sales_stage_history[-1]["source"] == "agent"
    assert session.next_action == "确认评估结果并推进合同"


def test_pipeline_fields_persist_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite3"
    store = SessionStore(db_path)
    session = store.get("wa-+61400000003")
    session.set_sales_stage("contract", source="agent", manual=True)
    session.next_action = "Send revised contract"
    session.next_action_manual = True
    session.follow_up_due_at = "2026-10-01T02:00:00+00:00"
    store.save(session)

    restored = SessionStore(db_path).get(session.session_id)

    assert restored.sales_stage == "contract"
    assert restored.sales_stage_manual is True
    assert restored.next_action == "Send revised contract"
    assert restored.next_action_manual is True
    assert restored.follow_up_due_at == "2026-10-01T02:00:00+00:00"
    assert restored.sales_stage_history[-1]["to"] == "contract"


def test_stage_never_moves_backwards_automatically() -> None:
    session = Session(
        session_id="wa-+61400000004",
        sales_stage="quotation",
        sales_stage_manual=False,
    )
    session.add_message("assistant", "Hello")

    session.sync_contact_fields()

    assert session.sales_stage == "quotation"


def test_agent_update_locks_stage_and_pipeline_summary_counts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator.sessions = store
    store.save(Session(session_id="wa-+61400000005"))

    updated = orchestrator.update_contact(
        "wa-+61400000005",
        sales_stage="site_assessment",
        next_action="Confirm site visit",
        follow_up_due_at="2020-01-01T00:00:00Z",
    )
    summary = orchestrator.pipeline_summary()

    assert updated["sales_stage"] == "site_assessment"
    assert updated["sales_stage_manual"] is True
    assert updated["next_action"] == "Confirm site visit"
    assert updated["follow_up_due_at"] == "2020-01-01T00:00:00+00:00"
    assert summary["overdue"] == 1
    assert next(
        item for item in summary["stages"] if item["code"] == "site_assessment"
    )["count"] == 1


def test_agent_can_release_stage_lock(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator.sessions = store
    session = Session(session_id="wa-+61400000006")
    session.set_sales_stage("qualified", source="agent", manual=True)
    store.save(session)

    updated = orchestrator.update_contact(
        session.session_id,
        sales_stage_auto=True,
    )

    assert updated["sales_stage_manual"] is False


def test_agent_can_set_and_release_priority(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator.sessions = store
    session = Session(session_id="wa-+61400000007")
    store.save(session)

    updated = orchestrator.update_contact(
        session.session_id,
        lead_temperature="hot",
    )

    assert updated["lead_temperature"] == "hot"
    assert updated["lead_temperature_manual"] is True

    released = orchestrator.update_contact(
        session.session_id,
        lead_temperature_auto=True,
    )

    assert released["lead_temperature_manual"] is False
