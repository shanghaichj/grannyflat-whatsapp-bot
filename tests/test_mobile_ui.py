from pathlib import Path

from grannyflat_bot.orchestrator import ChatOrchestrator


STATIC = Path(__file__).parents[1] / "src" / "grannyflat_bot" / "static"


def test_mobile_surfaces_do_not_link_to_desktop_admin_or_crm() -> None:
    chat = (STATIC / "mobile-chat.html").read_text(encoding="utf-8")
    crm = (STATIC / "mobile-crm.html").read_text(encoding="utf-8")

    assert 'id="btnFull"' not in chat
    assert 'location.href = "/admin"' not in chat
    assert 'href="/admin"' not in crm
    assert 'href="/crm/home"' not in crm
    assert 'href="/crm/tasks"' not in crm
    assert 'id="btnCrm"' in chat
    assert 'location.href = "/m/crm/home"' in chat


def test_mobile_priority_and_voice_translation_controls_exist() -> None:
    chat = (STATIC / "mobile-chat.html").read_text(encoding="utf-8")

    assert 'id="editPriority"' in chat
    assert "payload.lead_temperature" in chat
    assert 'id="btnMic"' in chat
    assert 'recognition.lang = "zh-CN"' in chat
    assert 'api("/api/translate/zh-en"' in chat


def test_slash_template_selection_keeps_command_for_backend() -> None:
    chat = (STATIC / "mobile-chat.html").read_text(encoding="utf-8")
    admin = (STATIC / "admin.html").read_text(encoding="utf-8")

    assert 'inputEl.value = "/" + t.key' in chat
    assert 'agentInput.value = "/" + t.key' in admin
    assert "inputEl.value = t.text" not in chat
    assert "agentInput.value = t.text" not in admin
    assert 'key: "role"' in chat
    assert 'key: "role"' in admin
    assert 'HXad1b8c385087a29613227481a2953d15' in chat
    assert 'HXad1b8c385087a29613227481a2953d15' in admin

    orchestrator = object.__new__(ChatOrchestrator)
    assert orchestrator._match_agent_template_command("/60s") == (
        "60s",
        ChatOrchestrator.LAYOUT_TEMPLATE_SIDS["60"],
    )
    assert orchestrator._match_agent_template_command("/role") == (
        "role",
        "HXad1b8c385087a29613227481a2953d15",
    )
