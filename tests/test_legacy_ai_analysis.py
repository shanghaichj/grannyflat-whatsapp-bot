"""老号码固定 AI 历史分析：更新需求字段、资格评分、AI 销售摘要。

覆盖需求：
- 取消 AI 指令分析后，``/api/legacy/analyze`` 走固定流程且不再需要 instruction；
- LLM 从真实历史聊天提取结构化字段，只填空、绝不覆盖已有事实；
- 人工锁定（lead_temperature_manual 等）不被 AI 覆盖；
- 模型不可用时自动退回确定性摘要（source=rules）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grannyflat_bot.legacy_analysis import (
    LegacyHistoryResult,
    _fill_only_patch,
    analyze_legacy_history,
    extract_history_patch,
)
from grannyflat_bot.session.store import Session, SessionStore


class _FakeLiveLLM:
    live = True

    def __init__(self, patch: dict | None = None, summary: str = "") -> None:
        self._patch = patch or {}
        self._summary = summary

    def chat_json(self, messages, temperature: float = 0.0):  # noqa: ANN001
        system = str(messages[0].get("content", ""))
        if "extract structured granny-flat requirements" in system:
            return {"patch": self._patch}
        return {"summary": self._summary}


class _FakeOfflineLLM:
    live = False


def _store(tmp_path: Path, sessions: list[Session]) -> SessionStore:
    store = SessionStore(tmp_path / "legacy.sqlite3")
    for session in sessions:
        store.save(session, touch=False)
    return store


def _session(session_id: str, messages: list[tuple[str, str]]) -> Session:
    session = Session(session_id=session_id)
    for role, content in messages:
        session.add_message(role, content)
    return session


def test_extract_history_patch_validates_types_and_enums() -> None:
    llm = _FakeLiveLLM(
        patch={
            "state": "vic",            # 规范化成大写
            "bedrooms": "2",           # 字符串数字 -> int
            "budget_aud_max": "95000", # 字符串 -> float
            "postcode": "abcd",        # 非法 -> 丢弃
            "delivery_type": "hover",  # 非枚举 -> 丢弃
            "suburb_or_lga": "  Box Hill  ",
        }
    )
    patch, error = extract_history_patch(
        ["looking for a 2 bed granny flat in box hill"], client=llm
    )
    assert error is None
    assert patch == {
        "state": "VIC",
        "bedrooms": 2,
        "budget_aud_max": 95000.0,
        "suburb_or_lga": "Box Hill",
    }


def test_extract_history_patch_reports_errors_instead_of_silence() -> None:
    class _BrokenLlm:
        live = True

        def chat_json(self, messages, temperature=0.0):
            raise RuntimeError("403 region blocked")

    patch, error = extract_history_patch(["hi"], client=_BrokenLlm())
    assert patch == {}
    assert error and "403" in error

    patch, error = extract_history_patch(["hi"], client=_FakeOfflineLLM())
    assert patch == {} and error == "LLM 未配置"
    patch, error = extract_history_patch([], client=_FakeLiveLLM(patch={"state": "NSW"}))
    assert patch == {} and error is None
    patch, error = extract_history_patch(["hi"], client=None)
    assert patch == {} and error == "LLM 未配置"


def test_local_extraction_trailing_major_city() -> None:
    """句尾大城市提及（如 "Rental, Sydney"）应本地提取出城市+州。"""
    from grannyflat_bot.legacy_analysis import infer_location

    candidate = infer_location(["Can I get more details?", "Rental, Sydney"])
    assert candidate.city == "Sydney"
    assert candidate.state == "NSW"

    candidate = infer_location(["We are based in Melbourne"])
    assert candidate.city == "Melbourne"
    assert candidate.state == "VIC"


def test_fill_only_patch_never_overwrites_existing_facts() -> None:
    session = _session("wa-test", [])
    session.requirements = session.requirements.apply_patch(
        {"bedrooms": 3}, source="manual", confidence=1.0
    )
    patch = _fill_only_patch(session.requirements, {"bedrooms": 2, "timeline": "3 months"})
    assert patch == {"timeline": "3 months"}


def test_analyze_updates_fields_qualifies_and_summarizes(tmp_path: Path) -> None:
    session = _session(
        "wa-61400000001",
        [
            ("user", "Hi, we are in VIC and want a 2 bedroom granny flat"),
            ("assistant", "Sure, may I have your suburb?"),
            ("user", "Box Hill, budget around 90000"),
        ],
    )
    store = _store(tmp_path, [session])
    llm = _FakeLiveLLM(
        patch={"bedrooms": 2, "budget_aud_max": 90000, "timeline": "2 months"},
        summary="维州 Box Hill 客户，2 卧，预算约 9 万澳元，两个月内动工。",
    )

    result = analyze_legacy_history(store, llm=llm)

    assert isinstance(result, LegacyHistoryResult)
    assert result.scanned == 1
    assert result.updated == 1
    assert result.qualified == 1
    assert result.summarized == 1
    assert "AI 分析完成" in result.message

    saved = store.get("wa-61400000001")
    assert saved.requirements.bedrooms == 2
    assert saved.requirements.budget_aud_max == 90000.0
    assert saved.requirements.timeline == "2 months"
    assert saved.requirements.field_sources["bedrooms"] == "ai_history_analysis"
    # 本地确定性提取补全州
    assert saved.state == "VIC"
    assert saved.qualification_score > 0
    assert saved.ai_summary == "维州 Box Hill 客户，2 卧，预算约 9 万澳元，两个月内动工。"
    assert saved.ai_summary_source == "llm"
    assert saved.ai_summary_updated_at
    assert saved.next_action  # 按阶段给出建议行动


def test_analyze_respects_manual_temperature_lock(tmp_path: Path) -> None:
    session = _session("wa-61400000002", [("user", "just looking")])
    session.lead_temperature = "hot"
    session.lead_temperature_manual = True
    store = _store(tmp_path, [session])

    analyze_legacy_history(store, llm=_FakeLiveLLM(summary="咨询中"))

    saved = store.get("wa-61400000002")
    assert saved.lead_temperature == "hot"  # 人工锁定不被 AI 覆盖
    assert saved.ai_summary_source == "llm"


def test_analyze_falls_back_to_rules_summary_without_live_llm(tmp_path: Path) -> None:
    session = _session("wa-61400000003", [("user", "NSW 3 bedrooms please")])
    store = _store(tmp_path, [session])

    result = analyze_legacy_history(store, llm=_FakeOfflineLLM())

    saved = store.get("wa-61400000003")
    assert result.summarized == 1
    assert saved.ai_summary  # 确定性摘要兜底
    assert saved.ai_summary_source == "rules"
    # 无 LLM 时需求字段未被瞎填
    assert saved.requirements.bedrooms is None


def test_analyze_selected_scope_only(tmp_path: Path) -> None:
    first = _session("wa-61400000004", [("user", "hi")])
    second = _session("wa-61400000005", [("user", "hello")])
    store = _store(tmp_path, [first, second])
    llm = _FakeLiveLLM(summary="范围测试")

    result = analyze_legacy_history(
        store, scope="selected", session_ids=["wa-61400000004"], llm=llm
    )

    assert result.scanned == 1
    assert store.get("wa-61400000005").ai_summary is None


def test_teacher_sessions_are_excluded(tmp_path: Path) -> None:
    teach = _session("teach-unit-test", [("user", "teach me")])
    real = _session("wa-61400000006", [("user", "hi")])
    store = _store(tmp_path, [teach, real])

    result = analyze_legacy_history(store, llm=_FakeLiveLLM(summary="x"))

    assert result.scanned == 1
    assert store.get("teach-unit-test").ai_summary is None


# ---------------------------------------------------------------------------
# API 契约：取消指令后 /api/legacy/analyze 不再要求 instruction；
# 新增单联系人 AI 摘要端点；CRM 页面提供号码切换。
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(monkeypatch):  # noqa: ANN001
    from fastapi.testclient import TestClient

    from grannyflat_bot.admin_auth import ADMIN_COOKIE_NAME, create_session_token
    from grannyflat_bot.app import app
    from grannyflat_bot.config import get_settings

    monkeypatch.setattr(
        get_settings(), "admin_password", "unit-test-legacy-analysis"
    )
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE_NAME, create_session_token("admin"))
    return client


def test_legacy_analyze_api_no_longer_requires_instruction(monkeypatch, authed_client) -> None:
    import grannyflat_bot.legacy_archive as legacy_archive

    def _stub(store, **kwargs):  # noqa: ANN001, ANN003
        return LegacyHistoryResult(
            scanned=2,
            updated=2,
            fields_updated=1,
            location_updated=1,
            builder_marked=0,
            qualified=2,
            summarized=2,
            llm_errors=0,
            skipped=0,
            message="stub",
        )

    monkeypatch.setattr(legacy_archive, "analyze_legacy_history", _stub)

    res = authed_client.post("/api/legacy/analyze", json={"scope": "all"})
    assert res.status_code == 202
    job_id = res.json()["job_id"]

    job = authed_client.get(f"/api/legacy/analyze/{job_id}").json()
    assert job["status"] in {"queued", "running", "completed"}

    # 旧的 instruction 字段不再是请求模型的一部分（多余字段被忽略而非必填）
    res2 = authed_client.post(
        "/api/legacy/analyze",
        json={"scope": "selected", "session_ids": [], "instruction": "legacy"},
    )
    assert res2.status_code == 400  # 空范围仍然被拦截


def test_legacy_ai_summary_endpoint_exists(authed_client) -> None:
    res = authed_client.post("/api/legacy/contacts/wa-does-not-exist/ai-summary")
    assert res.status_code == 404  # 路由存在且走存在性校验


def test_crm_page_contains_number_switch() -> None:
    html = Path(__file__).resolve().parents[1] / "src" / "grannyflat_bot" / "static" / "crm.html"
    text = html.read_text(encoding="utf-8")
    assert "workspace-picker" in text
    assert "api/legacy/contacts" in text
    # 新号码分支必须带 /api 前缀（回归：曾写成返回裸路径导致客户列表为空）。
    assert "'/api'+p" in text


def test_legacy_admin_page_matches_workbench_with_isolated_api() -> None:
    """老号码页面与工作台 UI 一致，但所有数据接口都走 /api/legacy 隔离命名空间。"""
    static_dir = Path(__file__).resolve().parents[1] / "src" / "grannyflat_bot" / "static"
    legacy = (static_dir / "legacy-admin.html").read_text(encoding="utf-8")
    workbench = (static_dir / "admin.html").read_text(encoding="utf-8")

    # 工作台核心 UI 模块在老号码页全部存在（功能对齐）
    for feature_id in (
        "ai-sales-card",          # AI 销售摘要卡
        "pipeline-filters",       # 销售管道筛选
        "adv-filters",            # 高级筛选
        "crm-drawer",             # CRM 抽屉
        "bulk-toolbar",           # 批量操作栏
        "f-sales-stage",          # 销售阶段编辑
        "btn-refresh-summary",    # 更新摘要按钮
        "agent-form",             # 客服输入
    ):
        assert feature_id in legacy, f"老号码页面缺少工作台模块: {feature_id}"
        assert feature_id in workbench

    # 数据接口全部隔离到 /api/legacy，且不依赖新号码任务系统
    assert "/api/legacy/contacts" in legacy
    assert '"api/tasks"' not in legacy.replace("/api/legacy/tasks", "")
    assert 'authFetch("/api/contacts' not in legacy
    assert 'fetch("/api/contacts' not in legacy
    # CRM 跳转带 source=legacy 参数
    assert "/crm?source=legacy" in legacy
    # 保留老号码专属 AI 历史分析栏
    assert "analysis-run" in legacy
    assert "/api/legacy/analyze" in legacy


def test_crm_page_reads_source_url_param() -> None:
    html = Path(__file__).resolve().parents[1] / "src" / "grannyflat_bot" / "static" / "crm.html"
    text = html.read_text(encoding="utf-8")
    assert "urlSource" in text
    assert "applySourceUI()" in text


# ---------------------------------------------------------------------------
# 通讯录顺序分析 + 进度回调
# ---------------------------------------------------------------------------


def _ordered_session(session_id: str, updated_at: str, with_messages: bool) -> Session:
    session = Session(session_id=session_id)
    session.updated_at = updated_at
    if with_messages:
        session.add_message("user", f"hello from {session_id}")
    return session


def _recording_store(tmp_path: Path, sessions: list[Session]):
    """包装真实 store，记录 save() 的调用顺序（用于验证分析顺序）。"""
    store = _store(tmp_path, sessions)
    saved: list[str] = []

    original_save = store.save

    def recorder(session, **kwargs):  # noqa: ANN001, ANN003, ANN202
        saved.append(str(getattr(session, "session_id", "")))
        return original_save(session, **kwargs)

    store.save = recorder  # type: ignore[method-assign]
    return store, saved


def test_analysis_follows_contact_list_order(tmp_path: Path) -> None:
    # B 最新消息在前，A 其次，C 无消息沉底 —— 与工作台通讯录显示顺序一致。
    s_a = _ordered_session("wa-order-a", "2026-09-20T10:00:00+00:00", True)
    s_b = _ordered_session("wa-order-b", "2026-09-25T10:00:00+00:00", True)
    s_c = _ordered_session("wa-order-c", "2026-09-27T10:00:00+00:00", False)
    store, saved = _recording_store(tmp_path, [s_a, s_b, s_c])

    result = analyze_legacy_history(
        store, scope="all", llm=_FakeLiveLLM(summary="x")
    )

    assert result.scanned == 3
    assert saved == ["wa-order-b", "wa-order-a", "wa-order-c"]


def test_selected_scope_keeps_frontend_order(tmp_path: Path) -> None:
    s_a = _ordered_session("wa-sel-a", "2026-09-25T10:00:00+00:00", True)
    s_b = _ordered_session("wa-sel-b", "2026-09-20T10:00:00+00:00", True)
    store, saved = _recording_store(tmp_path, [s_a, s_b])

    analyze_legacy_history(
        store,
        scope="selected",
        session_ids=["wa-sel-b", "wa-sel-a"],
        llm=_FakeLiveLLM(summary="x"),
    )

    # 用户手动圈选时保持前端传来的顺序，不做重排。
    assert saved == ["wa-sel-b", "wa-sel-a"]


def test_progress_callback_reports_counts(tmp_path: Path) -> None:
    sessions = [
        _ordered_session(f"wa-pg-{i}", f"2026-09-2{i}T10:00:00+00:00", True)
        for i in range(3)
    ]
    store = _store(tmp_path, sessions)
    events: list[tuple[int, int]] = []

    analyze_legacy_history(
        store,
        scope="all",
        llm=_FakeLiveLLM(summary="x"),
        progress=lambda done, total: events.append((done, total)),
    )

    assert events[0] == (0, 3)  # 开始时先报告总量
    assert [done for done, _ in events[1:]] == [1, 2, 3]  # 单调递增
    assert all(total == 3 for _, total in events)
