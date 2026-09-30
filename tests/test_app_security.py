from pathlib import Path

from fastapi.testclient import TestClient

from grannyflat_bot.app import app
from grannyflat_bot.config import get_settings


STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "grannyflat_bot" / "static"


def test_twilio_is_the_only_whatsapp_webhook() -> None:
    # Newer FastAPI versions keep included routers in this collection too.
    paths = {path for route in app.routes if (path := getattr(route, "path", None))}

    assert "/webhooks/twilio/whatsapp" in paths
    assert "/webhooks/whatsapp" not in paths
    assert "/crm" in paths
    assert "/crm/home" in paths
    assert "/crm/tasks" in paths
    assert "/m/crm" in paths
    assert "/m/crm/home" in paths


def test_public_surface_is_safe_by_default() -> None:
    # Deliberately avoid TestClient's lifespan context: startup owns the real
    # delayed-message worker and must never run during a no-message unit test.
    settings = get_settings()
    previous_web = settings.public_web_chat_enabled
    previous_password = settings.admin_password
    settings.public_web_chat_enabled = True
    settings.admin_password = "unit-test-admin-password"
    try:
        client = TestClient(app)
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json() == {
            "ok": True,
            "version": "0.3.5",
            "channel": "twilio_whatsapp",
        }
        root = client.get("/", follow_redirects=False)
        assert root.status_code in (302, 307)
        assert root.headers["location"] == "/admin"
        assert client.get("/crm").status_code == 200
        assert "销售 CRM" in client.get("/crm").text
        assert client.post("/api/chat", json={"message": "unit test"}).status_code == 404
        assert client.get("/api/tasks", follow_redirects=False).status_code in (
            302,
            303,
            307,
            401,
        )
    finally:
        settings.public_web_chat_enabled = previous_web
        settings.admin_password = previous_password


def test_crm_and_customer_service_surfaces_keep_the_required_boundaries() -> None:
    crm_html = (STATIC_DIR / "crm.html").read_text(encoding="utf-8")
    admin_html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    legacy_html = (STATIC_DIR / "legacy-admin.html").read_text(encoding="utf-8")
    teach_html = (STATIC_DIR / "teach.html").read_text(encoding="utf-8")
    dashboard_html = (STATIC_DIR / "crm-dashboard.html").read_text(encoding="utf-8")
    mobile_crm_html = (STATIC_DIR / "mobile-crm.html").read_text(encoding="utf-8")
    mobile_chat_html = (STATIC_DIR / "mobile-chat.html").read_text(encoding="utf-8")

    # CRM is a dedicated HubSpot-style sales page with an explicit legacy-number entry.
    assert 'href="/admin/old-number"' in crm_html
    assert "打开客服对话" in crm_html
    assert "未分配" not in crm_html
    assert 'id="agent-form"' not in crm_html

    # The existing service workspace keeps its operational messaging controls.
    assert 'id="btn-toggle-mode"' in admin_html
    assert 'id="btn-archive"' in admin_html
    assert 'id="agent-form"' in admin_html
    assert 'id="tplPop"' in admin_html
    assert 'id="crm-drawer"' in admin_html
    assert 'id="btn-toggle-crm"' in admin_html
    assert 'id="crm-detail-link"' in admin_html
    assert "/crm?session=" in admin_html
    assert "requestedSession" in crm_html
    assert "/admin?session=" in crm_html
    assert 'id="priority-filters" aria-label="优先级"' in admin_html
    assert 'data-priority="reply"' in admin_html
    assert "活跃客户" not in admin_html
    assert 'data-mode="archived"' not in admin_html
    assert '<label for="f-sales-queue">优先级</label>' in admin_html
    assert '<label for="f-sales-stage">销售状态</label>' in admin_html
    assert 'title="可人工修改销售状态"' in admin_html
    assert "销售队列" not in admin_html
    assert "<h3>优先级</h3>" in crm_html
    assert "销售队列" not in crm_html
    assert 'placeholder="搜索所有联系人和聊天内容"' in admin_html
    assert "开始新聊天" not in admin_html
    assert 'id="session-meta" hidden' in admin_html
    assert 'id="btn-toggle-q"' not in admin_html
    assert 'id="channel-pill">WhatsApp · 已连接' in admin_html
    assert 'id="f-customer-status" title="兼容状态字段" hidden' in admin_html
    assert 'id="f-workflow" title="兼容处理状态字段" hidden' in admin_html
    assert "address: normalizedAddress" in admin_html
    assert "address: normalizedAddress" in legacy_html
    assert '.avatar.priority-hot' in admin_html
    assert '.avatar.priority-warm' in admin_html
    assert '.avatar.priority-cold' in admin_html
    assert '.avatar.priority-non-target' in admin_html
    for level in range(1, 11):
        class_name = f".avatar.priority-level-{level}"
        assert class_name in admin_html
        assert class_name in legacy_html
        assert class_name in crm_html
        assert class_name in mobile_chat_html
    assert "function avatarPriorityLevel(c)" in admin_html
    assert "function avatarPriorityLevel(c)" in legacy_html
    assert "function avatarPriorityLevel(c)" in crm_html
    assert "function avatarPriorityLevel(c)" in mobile_chat_html
    assert "priority-level-${avatarPriorityLevel(c)}" in admin_html
    assert "priority-level-${avatarPriorityLevel(c)}" in crm_html
    assert '"avatar priority-level-" + avatarPriorityLevel(c)' in mobile_chat_html
    assert 'stageAutoOption.textContent = `自动：${actualStageLabel}`' in admin_html
    assert 'queueAutoOption.textContent = `自动：${actualPriorityLabel}`' in admin_html
    assert 'color: var(--wa-green-deep);' in admin_html
    assert 'color: var(--wa-muted); font-size: 0.74rem; font-weight: 500;' in admin_html
    assert 'summary.includes("非目标原因")' in admin_html
    assert '客服人工设置为非目标客户' in admin_html
    assert "CRM 客户卡" in admin_html
    # 老号码页面与工作台 UI/功能一致，但走隔离的 /api/legacy 命名空间，
    # 并保留专属 AI 历史分析栏（2026-09-27 重构后与 admin.html 同源）。
    assert 'id="channel-pill">WhatsApp · 已连接' in legacy_html
    assert 'id="f-customer-status" title="兼容状态字段" hidden' in legacy_html
    assert 'id="f-workflow" title="兼容处理状态字段" hidden' in legacy_html
    assert "老号码工作台" in legacy_html
    assert "数据库隔离" in legacy_html
    assert 'id="analysis-run"' in legacy_html
    assert 'placeholder="搜索所有联系人和聊天内容"' in legacy_html
    assert 'authFetch("/api/legacy/contacts' in legacy_html
    assert 'authFetch("/api/contacts' not in legacy_html
    assert "活跃客户" not in legacy_html
    assert 'data-mode="archived"' not in legacy_html
    assert 'data-bulk-action="vip"' not in admin_html
    assert 'data-bulk-action="vip"' not in legacy_html
    for contact_html in (admin_html, legacy_html):
        assert 'id="flt-contact-type" title="客户身份"' in contact_html
        assert 'select id="flt-tags" title="客服手工标签"' in contact_html
        assert 'id="f-contact-type" title="客户身份"' in contact_html
        assert 'id="f-tags" class="tag-input"' in contact_html
        assert 'placeholder="选择或输入标签，按 Enter 添加"' in contact_html
        assert 'role="combobox"' in contact_html
        assert 'id="ai-sales-card"' in contact_html
        assert 'id="crm-drawer"' in contact_html
        assert 'title="选择已有标签"' in contact_html
        assert 'id="f-tag-select"' not in contact_html
        assert 'placeholder="详细门牌地址（含邮编，AI 自动读取，无则人工输入）"' in contact_html
        assert 'id="f-postcode"' not in contact_html
        assert 'id="f-surname"' in contact_html
        assert 'id="f-given"' in contact_html
        assert 'c.code === "unknown" ? "客户身份" : c.label' in contact_html
        assert 'return fContactType.value || "unknown";' in contact_html
        assert "身份与自由标签严格分离" in contact_html
        assert 'id="btn-save-contact"' in contact_html
        assert 'btnSaveContact.addEventListener("click", saveContactForm)' in contact_html
        assert "setTimeout(() => { saveContactForm(); }, 450)" not in contact_html
        assert "scheduleAutoSave" not in contact_html
        assert "editingContact" in contact_html

    # CRM home and task center use the live task/contact APIs.
    assert "会议与跟进" in dashboard_html
    assert "全部任务" in dashboard_html
    assert "'/api/tasks'" in dashboard_html
    assert "'/api/contacts'" in dashboard_html
    assert 'class="task" href="/admin?session=' in dashboard_html
    assert 'href="/crm?session=' not in dashboard_html
    assert 'href="/crm">◎ 客户</a><a id="nav-tasks"' in dashboard_html
    assert 'href="/crm">◎ 客户</a><a href="/crm/tasks">☑ 任务</a>' in crm_html
    assert "◉ 旧号码" not in dashboard_html
    assert "◉ 旧号码" not in crm_html
    assert "老号码客户" not in crm_html
    assert "else if (contacts.length) await selectContact(contacts[0].session_id);" in admin_html

    # Mobile CRM home tracks meetings/follow-ups and opens the same chat records.
    assert "会议与跟进" in mobile_crm_html
    assert "待办事项" in mobile_crm_html
    assert "fetch('/api/tasks'" in mobile_crm_html
    assert "/m/chat?session=" in mobile_crm_html
    assert 'class="task" href="/m/chat?session=' in mobile_crm_html
    assert 'id="btnCrm"' in mobile_chat_html
    assert "requestedSession" in mobile_chat_html
    assert 'id="editNextAction"' in mobile_chat_html
    assert 'id="editFollowUpDue"' in mobile_chat_html
    assert "payload.next_action_auto = false" in mobile_chat_html
    assert "payload.follow_up_due_auto = false" in mobile_chat_html

    # Every operator page shares the same left rail; the retired demo has no entry.
    for html in (crm_html, admin_html, legacy_html, teach_html):
        assert 'aria-label="主功能"' in html
        assert 'href="/admin"' in html
        assert 'href="/crm/home"' in html
        assert 'href="/admin/old-number"' in html
        assert 'href="/teach"' in html
        assert "演示对话" not in html
