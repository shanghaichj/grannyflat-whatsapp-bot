"""自然语言批量发送指令：解析 → 预览 → 确认 → 限流队列发送。

客服在工作台指令框输入一句话，例如：

    给所有重点客户发送模版 60s
    给所有昆州的客户发送模版消息 60price

流程：
1. ``parse_command`` 规则解析出「模版名 + 筛选条件」，全部命中白名单才继续；
2. ``POST /api/broadcast/preview`` 干跑（dry-run）返回匹配人数、样例号码、
   跳过原因统计与 Twilio 激活状态，前端必须展示并二次确认；
3. ``POST /api/broadcast/execute``（confirm=true）建任务，后台逐条限流发送；
4. ``GET /api/broadcast/jobs/{id}`` 轮询进度。

安全设计：
- 模版名/筛选条件均为枚举白名单，拒绝任何自由文本拼接；
- 单任务人数上限 ``MAX_BROADCAST_RECIPIENTS``，超过必须缩小范围；
- 同时只允许一个广播任务（409 冲突）；
- 已发过同模版的人自动跳过，避免重复骚扰；
- 默认排除归档与 teach-* 会话；无号码的联系人跳过；
- 逐条间隔 ``BROADCAST_INTERVAL_SECONDS``，降低 WhatsApp 反垃圾风控概率。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from grannyflat_bot.admin_auth import require_admin
from grannyflat_bot.config import get_settings
from grannyflat_bot.session.contacts import CUSTOMER_STATUSES
from grannyflat_bot.session.store import Session

logger = logging.getLogger(__name__)
router = APIRouter()

BROADCAST_INTERVAL_SECONDS = 1.5  # 逐条发送间隔：Twilio/WhatsApp 反垃圾限流
MAX_BROADCAST_RECIPIENTS = 500    # 单任务人数上限
MAX_KEPT_JOBS = 20                # 内存中保留的最近任务数
SAMPLE_SIZE = 5                   # 预览样例号码个数

SUPPORTED_TEMPLATES_DOC = (
    "15s / 20s / 30s / 60s / 70s / 80s / 60price / 70price / introduce / shiping / address"
)
SUPPORTED_FILTERS_DOC = (
    "全部 / 重点客户 / 新线索 / 已联系 / 需求确认 / 方案沟通 / 报价 / 意向客户 / "
    "昆州 / 新州 / 维州 / 南澳 / 西澳 / 塔州 / 首领地 / 北领地"
)


class CommandError(ValueError):
    """指令无法解析：模版名或筛选条件不在白名单。"""


# ---- 规则解析 ---------------------------------------------------------------

# 模版白名单在 bind() 时从 ChatOrchestrator 注入（与客服 /60s 同一套映射）。
_TEMPLATE_SIDS: dict[str, str] = {}

# 从指令中剥除的连接词（按长度降序替换）。中英文都覆盖。
_FILLERS = (
    "给所有", "向所有", "对所有", "把所有",
    "发送模版消息", "发送模板消息", "发送模版", "发送模板", "模版消息", "模板消息",
    "所有", "全部", "发送", "发给", "发一下", "发",
    "模版", "模板", "消息", "信息", "短信",
    "客户", "联系人", "们", "的", "一个", "一下", "条",
    "sendtemplatemessagetoall", "sendtemplatemessageto", "sendmessagetoall",
    "sendmessageto", "sendmessagetocustomers", "sendmessageto",
    "sendtoall", "sendto", "send", "toall", "to",
    "templatemessage", "template", "message", "msg",
    "customers", "customer", "contacts", "contact", "all", "the",
)

_FILTERS: dict[str, dict[str, Any]] = {
    "all": {"label": "全部客户", "match": lambda s: True},
    "vip": {"label": "重点客户", "match": lambda s: s.customer_status == "vip"},
    "new_lead": {
        "label": "新线索",
        "match": lambda s: getattr(s, "sales_stage", None) == "new_lead",
    },
    "contacted": {
        "label": "已联系",
        "match": lambda s: getattr(s, "sales_stage", None) == "contacted",
    },
    "qualified": {
        "label": "需求确认",
        "match": lambda s: getattr(s, "sales_stage", None) == "qualified",
    },
    "design_discussion": {
        "label": "方案沟通",
        "match": lambda s: getattr(s, "sales_stage", None) == "design_discussion",
    },
    "quotation": {
        "label": "报价",
        "match": lambda s: getattr(s, "sales_stage", None) == "quotation",
    },
    "intended": {
        "label": "意向客户",
        "match": lambda s: s.lead_temperature in {"warm", "hot"},
    },
}

# 州筛选：中文别名 / 英文别名 → 州代码
_STATE_ALIASES = {
    "昆州": "QLD", "昆士兰": "QLD", "昆士兰州": "QLD", "qld": "QLD", "queensland": "QLD",
    "新州": "NSW", "新南威尔士": "NSW", "新南威尔士州": "NSW", "悉尼": "NSW",
    "nsw": "NSW", "sydney": "NSW",
    "维州": "VIC", "维多利亚": "VIC", "维多利亚州": "VIC", "墨尔本": "VIC",
    "vic": "VIC", "melbourne": "VIC", "victoria": "VIC",
    "南澳": "SA", "南澳大利亚": "SA", "南澳大利亚州": "SA", "阿德莱德": "SA",
    "sa": "SA", "adelaide": "SA", "southaustralia": "SA",
    "西澳": "WA", "西澳大利亚": "WA", "西澳大利亚州": "WA", "珀斯": "WA",
    "wa": "WA", "perth": "WA", "westernaustralia": "WA",
    "塔州": "TAS", "塔斯马尼亚": "TAS", "霍巴特": "TAS",
    "tas": "TAS", "hobart": "TAS", "tasmania": "TAS",
    "首领地": "ACT", "堪培拉": "ACT", "act": "ACT", "canberra": "ACT",
    "北领地": "NT", "达尔文": "NT", "nt": "NT", "darwin": "NT",
}

_FILTER_ALIASES: dict[str, str] = {
    "": "all", "全部": "all", "所有": "all", "全部客户": "all", "所有客户": "all",
    "all": "all",
    "重点客户": "vip", "vip": "vip",
    "新线索": "new_lead", "newlead": "new_lead",
    "已联系": "contacted", "contacted": "contacted",
    "需求确认": "qualified", "qualified": "qualified",
    "方案沟通": "design_discussion", "designdiscussion": "design_discussion",
    "报价": "quotation", "quotation": "quotation",
    "意向客户": "intended", "意向": "intended", "intended": "intended",
}
_FILTER_ALIASES.update(_STATE_ALIASES)

for _code in set(_STATE_ALIASES.values()):
    _FILTERS[_code] = {
        "label": _code,
        "match": (lambda c: lambda s: (s.state or "").upper() == c)(_code),
    }


def parse_command(command: str) -> dict[str, Any]:
    """规则解析指令，返回 {template_key, template_sid, filter_key, filter_label, raw}。

    只接受白名单内的模版名与筛选词，任何无法识别/歧义都抛 CommandError。
    """
    raw = (command or "").strip()
    if not raw:
        raise CommandError("指令不能为空")
    if not _TEMPLATE_SIDS:
        raise CommandError("模板库未初始化")
    # 去空白 + 小写：中英文混输、空格位置不同都能归一
    norm = re.sub(r"\s+", "", raw).lower()
    hits = sorted(
        {name for name in _TEMPLATE_SIDS if name in norm},
        key=len,
        reverse=True,
    )
    # 只保留最长命中：例如 "60price" 命中后不再把其中的片段当另一个模版
    maximal = [h for h in hits if not any(h != o and h in o for o in hits)]
    if not maximal:
        raise CommandError(
            f"没有识别到模版名（支持的模版：{SUPPORTED_TEMPLATES_DOC}）。"
            "示例：给所有重点客户发送模版 60s"
        )
    if len(maximal) > 1:
        raise CommandError(
            f"一条指令只能包含一个模版，当前识别到：{' / '.join(maximal)}"
        )
    key = maximal[0]
    rest = norm.replace(key, "", 1)
    # 子串扫描匹配筛选别名：连接词（给/所有/发送/的…）无需剥除，
    # 「重点客户」「昆州的客户」「send 60s to all vip customers」都能命中。
    hits: dict[str, list[str]] = {}
    for alias, fk in _FILTER_ALIASES.items():
        if alias and alias in rest:
            hits.setdefault(fk, []).append(alias)
    # 「全部/所有」是量词不是筛选：与其他条件同现时让位
    if "all" in hits and len(hits) > 1:
        hits.pop("all")
    if len(hits) > 1:
        raise CommandError("暂不支持组合筛选，一条指令只能用一个筛选条件")
    if hits:
        filter_key = next(iter(hits))
    else:
        stripped = rest
        for filler in sorted(_FILLERS, key=len, reverse=True):
            stripped = stripped.replace(filler, "")
        if stripped:
            raise CommandError(
                f"没有识别到筛选条件（收到：「{rest}」）。"
                f"支持的筛选：{SUPPORTED_FILTERS_DOC}"
            )
        filter_key = "all"
    return {
        "template_key": key,
        "template_sid": _TEMPLATE_SIDS[key],
        "filter_key": filter_key,
        "filter_label": _FILTERS[filter_key]["label"],
        "raw": raw,
    }


# ---- 目标筛选 ---------------------------------------------------------------

_ORCH: Any = None  # bind() 注入的 ChatOrchestrator


def bind(orchestrator: Any) -> None:
    """由 app.py 在创建 orchestrator 后调用：注入实例并同步模版白名单。"""
    global _ORCH
    _ORCH = orchestrator
    _TEMPLATE_SIDS.clear()
    _TEMPLATE_SIDS.update(orchestrator._agent_template_map())


def _get_orch() -> Any:
    if _ORCH is None:
        raise HTTPException(status_code=500, detail="broadcast module not initialized")
    return _ORCH


def _recipient_phone(session: Session) -> str | None:
    if session.whatsapp_phone:
        return session.whatsapp_phone
    if session.session_id.startswith("wa-"):
        return session.session_id.removeprefix("wa-")
    return None


def _display_name(session: Session) -> str:
    for attr in ("display_name", "name", "personal_name"):
        value = getattr(session, attr, None)
        if value:
            return str(value)
    return ""


def select_targets(
    orch: Any, parsed: dict[str, Any]
) -> tuple[list[Session], dict[str, int]]:
    """按筛选条件选人；返回 (目标会话, 跳过原因统计)。"""
    skipped = {
        "archived": 0,
        "no_phone": 0,
        "already_sent": 0,
        "filter_mismatch": 0,
    }
    matcher = _FILTERS[parsed["filter_key"]]["match"]
    targets: list[Session] = []
    for session in orch.sessions.list_all():
        if session.session_id.startswith("teach-"):
            continue
        if session.archived:
            skipped["archived"] += 1
            continue
        if not matcher(session):
            skipped["filter_mismatch"] += 1
            continue
        if not _recipient_phone(session):
            skipped["no_phone"] += 1
            continue
        if parsed["template_key"] in (session.sent_template_keys or []):
            skipped["already_sent"] += 1
            continue
        targets.append(session)
    return targets, skipped


def _twilio_live() -> bool:
    settings = get_settings()
    return bool(
        settings.twilio_configured
        and settings.twilio_whatsapp_enabled
        and getattr(settings, "twilio_whatsapp_active", True)
    )


# ---- 任务存储与发送 ---------------------------------------------------------

_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_jobs_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _counters(job: dict[str, Any]) -> dict[str, int]:
    return {
        "sent": sum(1 for r in job["results"] if r.get("sent")),
        "failed": sum(1 for r in job["results"] if not r.get("sent") and not r.get("recorded_only")),
        "recorded_only": sum(1 for r in job["results"] if r.get("recorded_only")),
        "processed": len(job["results"]),
    }


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["id"],
        "status": job["status"],
        "command": job["command"],
        "template_key": job["parsed"]["template_key"],
        "filter_label": job["parsed"]["filter_label"],
        "total": job["total"],
        "created_at": job["created_at"],
        **_counters(job),
    }


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    errors = [
        {"session_id": r.get("session_id"), "phone": r.get("phone"), "error": r.get("error")}
        for r in job["results"]
        if r.get("error")
    ]
    return {
        **_job_summary(job),
        "mode": job["mode"],
        "finished_at": job.get("finished_at"),
        "errors": errors[:50],
        "error_total": len(errors),
    }


async def _send_one(
    orch: Any, session_id: str, key: str, sid: str, *, agent_name: str
) -> dict[str, Any]:
    """向单个会话发送模版；Twilio 未激活时仅记录不真实发送。"""
    from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
    from grannyflat_bot.delivery_tracking import (
        CALLBACK_URL as DELIVERY_CALLBACK_URL,
        begin as delivery_begin,
        fix_message_at as delivery_fix_message_at,
        update as delivery_update,
    )

    session = orch.sessions.get(session_id)
    phone = _recipient_phone(session)
    base: dict[str, Any] = {"session_id": session_id, "phone": phone}

    if not _twilio_live():
        session.add_message(
            "agent",
            f"[broadcast recorded only: {key}]",
            extra={
                "agent_name": agent_name,
                "template_name": key,
                "template_sid": sid,
                "broadcast": True,
            },
        )
        orch.sessions.save(session, touch=False)
        return {**base, "sent": False, "recorded_only": True}

    channel = TwilioWhatsAppChannel()
    try:
        receipt_key = delivery_begin(session_id, message_at="")
        sent = await channel.send_content_template(
            to=phone,
            content_sid=sid,
            content_variables=None,
            status_callback=DELIVERY_CALLBACK_URL + "?receipt=" + receipt_key,
        )
        delivery_update(
            receipt_key,
            (sent or {}).get("status") or "queued",
            (sent or {}).get("sid"),
        )
        orch.mark_template_sent(
            session_id,
            {"sid": sid, "key": key, "name": key, "variables": None},
            message_sid=(sent or {}).get("sid"),
        )
        fresh = orch.sessions.get(session_id)
        delivery_fix_message_at(
            receipt_key, fresh.history[-1].get("at") if fresh.history else None
        )
        return {**base, "sent": True, "message_sid": (sent or {}).get("sid")}
    except Exception as exc:  # noqa: BLE001 — 逐条失败不影响整批任务
        logger.exception("broadcast send failed for %s", session_id)
        session.add_message(
            "agent",
            f"[broadcast failed: {key}] {str(exc)[:160]}",
            extra={
                "agent_name": agent_name,
                "template_name": key,
                "template_sid": sid,
                "broadcast": True,
            },
        )
        orch.sessions.save(session, touch=False)
        return {**base, "sent": False, "error": str(exc)[:200]}


async def _run_job(job_id: str, orch: Any, agent_name: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
    job["status"] = "running"
    parsed = job["parsed"]
    try:
        for index, session_id in enumerate(job["target_ids"]):
            result = await _send_one(
                orch,
                session_id,
                parsed["template_key"],
                parsed["template_sid"],
                agent_name=agent_name,
            )
            job["results"].append(result)
            if index < job["total"] - 1:
                await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
        job["status"] = "completed"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — 任务级兜底，状态必须收敛
        logger.exception("broadcast job %s crashed", job_id)
        job["status"] = "failed"
        job["fatal_error"] = str(exc)[:300]
    finally:
        job["finished_at"] = _utc_now()


def _spawn_job(job_id: str, orch: Any, agent_name: str) -> None:
    """在独立工作线程里跑广播任务（自带事件循环）。

    线程不被 HTTP 请求作用域管理：请求返回/取消不会杀掉正在发送的任务；
    与 legacy_analysis 的线程模型一致。Twilio 通道每次发送新建 httpx 客户端、
    回执存储逐次打开 sqlite 连接，跨线程/跨循环均安全。
    """

    def _target() -> None:
        try:
            asyncio.run(_run_job(job_id, orch, agent_name))
        except Exception:  # noqa: BLE001 — 状态必须收敛，不能让线程静默死亡
            logger.exception("broadcast job %s crashed in worker thread", job_id)
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is not None and job["status"] in {"queued", "running"}:
                job["status"] = "failed"
                job["fatal_error"] = "worker thread crashed"
                job["finished_at"] = _utc_now()

    threading.Thread(
        target=_target, name=f"broadcast-{job_id}", daemon=True
    ).start()


def _active_job_id() -> str | None:
    with _jobs_lock:
        for job_id, job in reversed(_jobs.items()):
            if job["status"] in {"queued", "running"}:
                return job_id
    return None


# ---- API --------------------------------------------------------------------

class BroadcastRequest(BaseModel):
    command: str
    confirm: bool = False
    agent_name: str = Field(default="客服", max_length=32)


def _parse_and_select(body: BroadcastRequest) -> tuple[dict[str, Any], list[Session], dict[str, int]]:
    orch = _get_orch()
    try:
        parsed = parse_command(body.command)
    except CommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    targets, skipped = select_targets(orch, parsed)
    if len(targets) > MAX_BROADCAST_RECIPIENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"匹配到 {len(targets)} 位客户，超过单任务上限 "
                f"{MAX_BROADCAST_RECIPIENTS} 人。请用更精确的筛选（如州+重点客户）缩小范围。"
            ),
        )
    return parsed, targets, skipped


@router.post("/api/broadcast/preview")
async def preview_broadcast(
    body: BroadcastRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """干跑：只解析和统计，不发送。前端必须展示结果并二次确认。"""
    parsed, targets, skipped = _parse_and_select(body)
    live = _twilio_live()
    sample = [
        {
            "session_id": s.session_id,
            "phone": _recipient_phone(s),
            "name": _display_name(s),
            "state": s.state or "",
            "customer_status": CUSTOMER_STATUSES.get(s.customer_status, s.customer_status),
        }
        for s in targets[:SAMPLE_SIZE]
    ]
    return {
        "ok": True,
        "parsed": parsed,
        "matched": len(targets),
        "sample": sample,
        "skipped": skipped,
        "mode": "live" if live else "recorded_only",
        "warnings": [] if live else ["Twilio 未激活：执行后仅在工作台记录，不会真实发送"],
        "interval_seconds": BROADCAST_INTERVAL_SECONDS,
        "estimated_seconds": int(len(targets) * BROADCAST_INTERVAL_SECONDS),
        "max_recipients": MAX_BROADCAST_RECIPIENTS,
    }


@router.post("/api/broadcast/execute", status_code=202)
async def execute_broadcast(
    body: BroadcastRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """确认后创建广播任务并后台限流发送；同一时刻只允许一个任务。"""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="缺少确认：请先 preview 并带 confirm=true 执行")
    active = _active_job_id()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"已有广播任务进行中（{active}），请等待完成后再发",
        )
    parsed, targets, _skipped = _parse_and_select(body)
    if not targets:
        raise HTTPException(status_code=400, detail="当前筛选条件下没有可发送的联系人")
    orch = _get_orch()
    job_id = uuid4().hex[:12]
    job = {
        "id": job_id,
        "command": parsed["raw"],
        "parsed": parsed,
        "target_ids": [s.session_id for s in targets],
        "total": len(targets),
        "mode": "live" if _twilio_live() else "recorded_only",
        "status": "queued",
        "results": [],
        "created_at": _utc_now(),
        "finished_at": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        while len(_jobs) > MAX_KEPT_JOBS:
            _jobs.popitem(last=False)
    _spawn_job(job_id, orch, body.agent_name)
    return {"ok": True, "job_id": job_id, "total": job["total"], "mode": job["mode"]}


@router.get("/api/broadcast/jobs")
async def list_broadcast_jobs(_: str = Depends(require_admin)) -> dict[str, Any]:
    with _jobs_lock:
        jobs = list(reversed(_jobs.values()))
    return {"jobs": [_job_summary(j) for j in jobs]}


@router.get("/api/broadcast/jobs/{job_id}")
async def get_broadcast_job(
    job_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return _job_view(job)
