import os

os.environ["MOCK_LLM"] = "1"

from grannyflat_bot.config import get_settings
from grannyflat_bot.export import SessionExporter, get_recording_state
from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.session.store import Session, SessionStore


def test_exporter_writes_three_files(tmp_path):
    get_settings.cache_clear()
    exporter = SessionExporter(export_root=tmp_path / "exports")
    session = Session(session_id="test-export-1")
    session.requirements = RequirementState(
        state="VIC",
        bedrooms=2,
        delivery_type="turnkey",
        approx_floor_area_m2=60,
    )
    session.history = [
        {"role": "user", "content": "Quote please"},
        {"role": "assistant", "content": "Sure"},
    ]
    session.last_quote = {
        "currency": "AUD",
        "can_quote": True,
        "subtotal": 124000,
        "range_low": 100000,
        "range_high": 140000,
        "line_items": [{"label": "Base", "amount_aud": 80000}],
    }

    paths = exporter.export_session(session, channel="web", locale="bilingual")
    assert paths.profile_md.exists()
    assert paths.quote_xlsx.exists()
    assert paths.transcript_json.exists()
    md = paths.profile_md.read_text(encoding="utf-8")
    assert "VIC" in md
    assert "客户画像" in md


def test_recording_toggle_and_auto_export(tmp_path):
    get_settings.cache_clear()
    get_recording_state().set_enabled(True)
    store = SessionStore(db_path=tmp_path / "s.sqlite3")
    exporter = SessionExporter(export_root=tmp_path / "exports")
    orch = ChatOrchestrator(sessions=store, exporter=exporter)

    r1 = orch.handle("rec-1", "NSW 2 bed turnkey quote please")
    assert r1.exports is not None
    assert (tmp_path / "exports" / "rec-1" / "user_profile.md").exists()

    get_recording_state().set_enabled(False)
    r2 = orch.handle("rec-1", "another message")
    assert r2.exports is None

    get_recording_state().clear_override()
