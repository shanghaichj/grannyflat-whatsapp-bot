"""自然语言批量发送指令：规则解析、预览/执行/进度 API 契约。

覆盖需求：
- 指令框输入「给所有重点客户发送模版 60s」等自然语言，规则解析出模版+筛选；
- 模版名与筛选条件全部走白名单，无法识别/歧义/超上限都被拒绝；
- preview 干跑返回人数、样例、跳过原因，Twilio 未激活时提示仅记录；
- execute 需要 confirm=true，同一时刻只允许一个任务；
- 已发过同模版、归档、无号码的联系人自动跳过；
- 发送在工作台历史留痕（recorded_only 模式不真实发送）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grannyflat_bot import broadcast
from grannyflat_bot.broadcast import CommandError, parse_command
from grannyflat_bot.session.store import Session, SessionStore

TEMPLATE_MAP = {
    "15s": "HX15", "20s": "HX20", "30s": "HX30",
    "60s": "HX60S", "70s": "HX70S", "80s": "HX80S",
    "60price": "HXP60", "70price": "HXP70",
    "introduce": "HXI1", "shiping": "HXI2", "address": "HXI3",
}


class _FakeOrchestrator:
    """最小 orchestrator：sessions + 模版映射 + mark_template_sent 记录。"""

    def __init__(self, store: SessionStore) -> None:
        self.sessions = store
        self.marked: list[tuple[str, str, str | None]] = []

    def _agent_template_map(self) -> dict[str, str]:
        return dict(TEMPLATE_MAP)

    def mark_template_sent(self, session_id, template, *, message_sid=None):  # noqa: ANN001
        self.marked.append((session_id, template.get("key"), message_sid))


def _wa(num: str, **kwargs) -> Session:  # noqa: ANN003
    session = Session(session_id=f"wa-614{num}")
    for key, value in kwargs.items():
        setattr(session, key, value)
    return session


@pytest.fixture(autouse=True)
def _template_whitelist() -> None:
    """解析测试不依赖 app 导入：直接注入模版白名单。"""
    broadcast._TEMPLATE_SIDS.clear()
    broadcast._TEMPLATE_SIDS.update(TEMPLATE_MAP)
    yield
    broadcast._TEMPLATE_SIDS.clear()


@pytest.fixture
def orch(tmp_path: Path) -> _FakeOrchestrator:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save(_wa("11111111", customer_status="vip", state="QLD"), touch=False)
    store.save(_wa("22222222", customer_status="vip", state="NSW"), touch=False)
    store.save(_wa("33333333", lead_temperature="warm", state="QLD"), touch=False)
    already = _wa("44444444", customer_status="vip", state="QLD")
    already.sent_template_keys.append("60s")
    store.save(already, touch=False)
    archived = _wa("55555555", customer_status="vip", state="QLD", archived=True)
    store.save(archived, touch=False)
    store.save(_wa("66666666", customer_status="vip", state="QLD"), touch=False)
    store.save(Session(session_id="web-no-phone", customer_status="vip"), touch=False)
    store.save(Session(session_id="teach-unit-test"), touch=False)
    fake = _FakeOrchestrator(store)
    broadcast.bind(fake)
    return fake


@pytest.fixture
def authed_client(monkeypatch):  # noqa: ANN001
    from grannyflat_bot.admin_auth import ADMIN_COOKIE_NAME, create_session_token
    from grannyflat_bot.app import app
    from grannyflat_bot.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_password", "unit-test-broadcast")
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE_NAME, create_session_token("admin"))
    return client


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------

def test_parse_chinese_vip_command() -> None:
    parsed = parse_command("给所有重点客户发送模版 60s")
    assert parsed["template_key"] == "60s"
    assert parsed["template_sid"] == "HX60S"
    assert parsed["filter_key"] == "vip"
    assert parsed["filter_label"] == "重点客户"


def test_parse_state_alias_command() -> None:
    parsed = parse_command("给所有昆州的客户发送模版消息 60price")
    assert parsed["template_key"] == "60price"
    assert parsed["filter_key"] == "QLD"

    assert parse_command("给所有昆士兰客户发送模版 60s")["filter_key"] == "QLD"
    assert parse_command("给所有悉尼客户发送模版 60s")["filter_key"] == "NSW"
    assert parse_command("给所有墨尔本客户发送模版 60s")["filter_key"] == "VIC"
    assert parse_command("给所有南澳客户发送模版 60s")["filter_key"] == "SA"


def test_parse_english_and_stage_commands() -> None:
    assert parse_command("send 60s to all vip customers")["filter_key"] == "vip"
    assert parse_command("给所有新线索发送模版 introduce")["filter_key"] == "new_lead"
    assert parse_command("给所有意向客户发送模版 70s")["filter_key"] == "intended"
    assert parse_command("给所有客户发送模版 80s")["filter_key"] == "all"


def test_parse_rejects_unknown_template() -> None:
    with pytest.raises(CommandError) as exc:
        parse_command("给所有重点客户发送模版 999s")
    assert "模版" in str(exc.value)

    with pytest.raises(CommandError):
        parse_command("给所有重点客户发个消息")


def test_parse_rejects_two_templates() -> None:
    with pytest.raises(CommandError) as exc:
        parse_command("给所有客户发送 60s 和 60price")
    assert "只能包含一个模版" in str(exc.value)


def test_parse_rejects_unknown_filter() -> None:
    with pytest.raises(CommandError) as exc:
        parse_command("给火星客户发送模版 60s")
    assert "筛选" in str(exc.value)


def test_parse_all_quantifier_is_not_combined_filter() -> None:
    # 「所有」是量词：与其他条件同现时不算组合筛选
    assert parse_command("给所有昆州客户发送模版 60s")["filter_key"] == "QLD"
    assert parse_command("给所有客户发送模版 60s")["filter_key"] == "all"


# ---------------------------------------------------------------------------
# 预览 API：选人、跳过统计、recorded_only 提示
# ---------------------------------------------------------------------------

def test_preview_vip_selection(authed_client, orch) -> None:
    res = authed_client.post(
        "/api/broadcast/preview", json={"command": "给所有重点客户发送模版 60s"}
    )
    assert res.status_code == 200
    data = res.json()
    # vip: 1111(QLD) + 2222(NSW) + 6666(QLD) 可发；4444 已发过 60s 跳过；
    # 5555 归档跳过；web-no-phone 无号码跳过
    assert data["matched"] == 3
    assert data["parsed"]["filter_label"] == "重点客户"
    assert data["skipped"]["already_sent"] == 1
    assert data["skipped"]["archived"] == 1
    assert data["skipped"]["no_phone"] == 1
    assert len(data["sample"]) == 3
    assert data["mode"] in {"live", "recorded_only"}


def test_preview_state_filter(authed_client, orch) -> None:
    res = authed_client.post(
        "/api/broadcast/preview", json={"command": "给所有昆州的客户发送模版 60s"}
    )
    assert res.status_code == 200
    data = res.json()
    # QLD: 1111(vip) + 3333(warm) + 6666(vip) 可发；4444 已发过跳过；5555 归档跳过
    assert data["matched"] == 3
    assert data["parsed"]["filter_key"] == "QLD"


def test_preview_bad_command_returns_400(authed_client, orch) -> None:
    res = authed_client.post(
        "/api/broadcast/preview", json={"command": "随便说点什么"}
    )
    assert res.status_code == 400
    assert "模版" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 执行 API：确认、幂等跳过、任务进度、单任务并发限制
# ---------------------------------------------------------------------------

def test_execute_requires_confirm(authed_client, orch) -> None:
    res = authed_client.post(
        "/api/broadcast/execute",
        json={"command": "给所有重点客户发送模版 60s"},
    )
    assert res.status_code == 400


def test_execute_recorded_only_flow(authed_client, orch, monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "_twilio_live", lambda: False)
    monkeypatch.setattr(broadcast, "BROADCAST_INTERVAL_SECONDS", 0.01)

    res = authed_client.post(
        "/api/broadcast/execute",
        json={"command": "给所有重点客户发送模版 60s", "confirm": True},
    )
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    assert res.json()["total"] == 3

    for _ in range(100):
        job = authed_client.get(f"/api/broadcast/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)  # 后台任务跑在 TestClient 门户线程的事件循环里
    else:
        pytest.fail("广播任务没有在预期时间内完成")

    assert job["status"] == "completed"
    assert job["processed"] == 3
    assert job["recorded_only"] == 3
    assert job["sent"] == 0

    # recorded_only 也应在工作台历史留痕
    history = " ".join(
        m.get("content", "") for m in orch.sessions.get("wa-61411111111").history
    )
    assert "[broadcast recorded only: 60s]" in history
    # recorded_only 不真实发送，因此不写入 sent_template_keys（避免挡住将来的真实发送）
    assert "60s" not in orch.sessions.get("wa-61411111111").sent_template_keys


def test_execute_conflict_when_job_running(authed_client, monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "_twilio_live", lambda: False)
    started = asyncio.Event()

    async def _slow_run(job_id, orch, agent_name):  # noqa: ANN001
        started.set()
        await asyncio.sleep(5)

    monkeypatch.setattr(broadcast, "_run_job", _slow_run)
    res = authed_client.post(
        "/api/broadcast/execute",
        json={"command": "给所有重点客户发送模版 60s", "confirm": True},
    )
    assert res.status_code == 202

    res2 = authed_client.post(
        "/api/broadcast/execute",
        json={"command": "给所有昆州客户发送模版 60price", "confirm": True},
    )
    assert res2.status_code == 409
    assert "进行中" in res2.json()["detail"]


# ---------------------------------------------------------------------------
# 前端结构
# ---------------------------------------------------------------------------

def test_admin_page_contains_command_bar() -> None:
    html = (Path(__file__).resolve().parents[1] / "src" / "grannyflat_bot" / "static" / "admin.html")
    text = html.read_text(encoding="utf-8")
    assert 'id="command-input"' in text
    assert 'id="command-run"' in text
    assert 'id="bc-overlay"' in text
    assert "/api/broadcast/preview" in text
    assert "/api/broadcast/execute" in text
