from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from grannyflat_bot.admin_auth import (
    ADMIN_COOKIE_NAME,
    create_session_token,
    require_admin,
    session_status,
    verify_admin_credentials,
)
from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
from grannyflat_bot.channels.base import OutboundMessage
from grannyflat_bot.config import get_settings
from grannyflat_bot.delivery_tracking import (
    CALLBACK_URL as DELIVERY_CALLBACK_URL,
    begin as delivery_begin,
    fix_message_at as delivery_fix_message_at,
    install_callback as install_delivery_callback,
    send_tracked,
    update as delivery_update,
)
from grannyflat_bot.export import get_recording_state
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.legacy_archive import router as legacy_archive_router
from grannyflat_bot.broadcast import bind as bind_broadcast
from grannyflat_bot.broadcast import router as broadcast_router

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="Granny Flat Sales Bot", version="0.3.5")
app.include_router(legacy_archive_router)
orchestrator = ChatOrchestrator()
app.include_router(broadcast_router)
bind_broadcast(orchestrator)
twilio_channel = TwilioWhatsAppChannel()

_address_followup_worker_task: asyncio.Task[None] | None = None


# 模拟客服阅读和手工输入：基础阅读停顿 + 按回复长度增加输入时间。
_REPLY_READ_PAUSE_SECONDS = 4.0
_REPLY_TYPING_CHARS_PER_SECOND = 8.0
_MIN_REPLY_DELAY_SECONDS = 8.0
_MAX_REPLY_DELAY_SECONDS = 25.0
# Twilio webhook 等待 TwiML 的安全窗口约15秒；超过该窗口的延时必须走后台队列异步发送。
_WEBHOOK_SYNC_DELAY_LIMIT_SECONDS = 15.0


def _reply_delay_seconds(reply: str) -> float:
    """按文本长度模拟客服阅读与输入，限制在 8–25 秒。"""
    length = len((reply or "").strip())
    estimated = _REPLY_READ_PAUSE_SECONDS + (
        length / _REPLY_TYPING_CHARS_PER_SECOND
    )
    return round(
        max(_MIN_REPLY_DELAY_SECONDS, min(_MAX_REPLY_DELAY_SECONDS, estimated)),
        1,
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _send_due_address_followups() -> None:
    """发送已到期的 60s 户型卡片；状态持久化，服务重启后仍可继续。"""
    now = datetime.now(timezone.utc)
    template = {
        "sid": orchestrator.LAYOUT_TEMPLATE_SIDS["60"],
        "size": "60",
        "key": "60s",
        "name": "60s",
        "variables": None,
    }
    for session in orchestrator.sessions.list_all():
        due = _parse_datetime(session.address_followup_due_at)
        if not due or due > now:
            continue
        if session.human_mode or "60s" in session.sent_template_keys:
            session.address_followup_due_at = None
            session.address_followup_attempts = 0
            orchestrator.sessions.save(session)
            continue
        recipient = session.whatsapp_phone or session.session_id.removeprefix("wa-")
        if not recipient:
            session.address_followup_due_at = None
            orchestrator.sessions.save(session)
            continue
        try:
            # 模板回执：先建占位回执（message_at 发送后回填），模板消息入历史后即可对账
            receipt_key = delivery_begin(session.session_id, message_at="")
            sent = await twilio_channel.send_content_template(
                to=recipient,
                content_sid=template["sid"],
                content_variables=None,
                status_callback=DELIVERY_CALLBACK_URL + "?receipt=" + receipt_key,
            )
            delivery_update(receipt_key, (sent or {}).get("status") or "queued", (sent or {}).get("sid"))
            orchestrator.mark_template_sent(
                session.session_id,
                template,
                message_sid=(sent or {}).get("sid"),
            )
            fresh = orchestrator.sessions.get(session.session_id)
            delivery_fix_message_at(
                receipt_key, fresh.history[-1].get("at") if fresh.history else None
            )
            logger.info(
                "10-minute 60s follow-up sent to=%s msg=%s",
                recipient,
                (sent or {}).get("sid"),
            )
        except Exception:
            session.address_followup_attempts += 1
            if session.address_followup_attempts >= 3:
                session.address_followup_due_at = None
            else:
                session.address_followup_due_at = (
                    now + timedelta(minutes=5)
                ).isoformat()
            orchestrator.sessions.save(session)
            logger.exception(
                "10-minute 60s follow-up failed for %s (attempt %d)",
                session.session_id,
                session.address_followup_attempts,
            )


async def _send_due_bot_replies() -> None:
    """发送延时的长回复；待发内容存入会话，服务重启后仍可继续。"""
    now = datetime.now(timezone.utc)
    for session in orchestrator.sessions.list_all():
        if not session.pending_bot_replies:
            continue
        if session.human_mode:
            session.pending_bot_replies.clear()
            orchestrator.sessions.save(session, touch=False)
            continue
        pending = session.pending_bot_replies[0]
        due = _parse_datetime(pending.get("due_at"))
        if not due or due > now:
            continue
        recipient = session.whatsapp_phone or session.session_id.removeprefix("wa-")
        if not recipient:
            session.pending_bot_replies.pop(0)
            orchestrator.sessions.save(session, touch=False)
            continue
        template = pending.get("template")
        if template:
            try:
                receipt_key = delivery_begin(session.session_id, message_at="")
                sent = await twilio_channel.send_content_template(
                    to=recipient,
                    content_sid=template["sid"],
                    content_variables=template.get("variables"),
                    status_callback=DELIVERY_CALLBACK_URL + "?receipt=" + receipt_key,
                )
                delivery_update(receipt_key, (sent or {}).get("status") or "queued", (sent or {}).get("sid"))
                orchestrator.mark_template_sent(
                    session.session_id, template,
                    message_sid=(sent or {}).get("sid"),
                )
                fresh = orchestrator.sessions.get(session.session_id)
                delivery_fix_message_at(
                    receipt_key, fresh.history[-1].get("at") if fresh.history else None
                )
            except Exception:
                logger.exception("Delayed template send failed for %s", session.session_id)
            pending["template"] = None
            orchestrator.sessions.save(session, touch=False)
        if session.human_mode or not session.pending_bot_replies or session.pending_bot_replies[0] is not pending:
            continue
        try:
            message_at = next(
                (
                    m.get("at")
                    for m in reversed(session.history)
                    if m.get("role") == "assistant" and m.get("content") == pending["text"]
                ),
                None,
            )
            await send_tracked(twilio_channel, session, text=str(pending["text"]), message_at=message_at)
            if not session.pending_bot_replies or session.pending_bot_replies[0] is not pending:
                continue
            session.pending_bot_replies.pop(0)
            if session.address_followup_due_at:
                session.address_followup_due_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat()
            orchestrator.sessions.save(session, touch=False)
            logger.info("Delayed bot reply sent to %s", recipient)
        except Exception:
            if not session.pending_bot_replies or session.pending_bot_replies[0] is not pending:
                continue
            pending["attempts"] = int(pending.get("attempts") or 0) + 1
            if pending["attempts"] >= 3:
                session.pending_bot_replies.pop(0)
            else:
                pending["due_at"] = (now + timedelta(minutes=1)).isoformat()
            orchestrator.sessions.save(session, touch=False)
            logger.exception("Delayed bot reply failed for %s", session.session_id)


async def _address_followup_worker() -> None:
    while True:
        try:
            await _send_due_address_followups()
            await _send_due_bot_replies()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Address follow-up worker iteration failed")
        await asyncio.sleep(2)


@app.on_event("startup")
async def _start_address_followup_worker() -> None:
    global _address_followup_worker_task
    settings = get_settings()
    if settings.production:
        from grannyflat_bot.preflight import production_config_errors

        errors = production_config_errors(settings)
        if errors:
            raise RuntimeError(
                "Production preflight failed: " + ", ".join(errors)
            )
    # In local/demo mode the Twilio master switch is off. Do not let the
    # scheduler consume retries or mutate pending customer messages while the
    # operator is only reviewing the workbench.
    if not settings.twilio_whatsapp_active:
        return
    if _address_followup_worker_task is None or _address_followup_worker_task.done():
        _address_followup_worker_task = asyncio.create_task(_address_followup_worker())


@app.on_event("shutdown")
async def _stop_address_followup_worker() -> None:
    global _address_followup_worker_task
    if _address_followup_worker_task is not None:
        _address_followup_worker_task.cancel()
        try:
            await _address_followup_worker_task
        except asyncio.CancelledError:
            pass
        _address_followup_worker_task = None


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "ok": True,
        "version": "0.3.5",
        "channel": "twilio_whatsapp",
    }


@app.get("/api/admin/diagnostics")
async def admin_diagnostics(_: str = Depends(require_admin)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "environment": settings.app_environment,
        "mock_llm": not settings.use_live_llm,
        "model": settings.openai_model,
        "twilio_configured": settings.twilio_configured,
        "twilio_whatsapp_enabled": settings.twilio_whatsapp_enabled,
        "twilio_whatsapp_active": settings.twilio_whatsapp_active,
        "twilio_validate_webhook": settings.twilio_validate_webhook,
        "admin_auth_enabled": settings.admin_auth_enabled,
        "public_web_chat_enabled": settings.public_web_chat_enabled,
        "session_recording": get_recording_state().status(),
    }


@app.get("/api/knowledge/health")
async def knowledge_health(_: str = Depends(require_admin)) -> dict[str, Any]:
    from grannyflat_bot.knowledge.audit import audit_knowledge

    return audit_knowledge()


class RecordingToggle(BaseModel):
    enabled: bool


@app.get("/api/recording")
async def get_recording(_: str = Depends(require_admin)) -> dict[str, Any]:
    return get_recording_state().status()


@app.post("/api/recording")
async def set_recording(
    body: RecordingToggle, _: str = Depends(require_admin)
) -> dict[str, Any]:
    get_recording_state().set_enabled(body.enabled)
    return get_recording_state().status()



class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    locale: str = "en"  # web: bilingual | en


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    questionnaire: dict[str, Any] = Field(default_factory=dict)
    quote: dict[str, Any] | None = None
    escalate_to_human: bool = False
    escalation_reason: str | None = None
    exports: dict[str, Any] | None = None
    bot_paused: bool = False
    # 提问试用：客服中文输入 / 客户英文输入的双语展示
    user_text_zh: str | None = None
    user_text_en: str | None = None
    user_translated: bool = False


class ContactUpdate(BaseModel):
    name: str | None = None  # 手动锁定完整显示名
    personal_name: str | None = None  # 仅改个人姓名，仍按城市/州规则生成显示名
    surname: str | None = None  # 姓，保持与名分开
    given: str | None = None  # 名，保持与姓分开
    state: str | None = None
    city: str | None = None
    postcode: str | None = None
    address: str | None = None
    contact_type: str | None = None
    customer_status: str | None = None
    customer_status_auto: bool | None = None
    tags: list[str] | None = None
    workflow_status: str | None = None
    sales_stage: str | None = None
    sales_stage_auto: bool | None = None
    lead_temperature: str | None = None
    lead_temperature_auto: bool | None = None
    next_action: str | None = None
    next_action_auto: bool | None = None
    follow_up_due_at: str | None = None
    follow_up_due_auto: bool | None = None
    category: str | None = None  # legacy alias for contact_type / workflow


class HumanModeRequest(BaseModel):
    enabled: bool


class ArchiveRequest(BaseModel):
    archived: bool = True


class AgentMessageRequest(BaseModel):
    message: str
    agent_name: str = "客服"


class TranslateRequest(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    """The former public demo has been retired; enter the service workspace."""
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/admin", response_class=HTMLResponse)
async def admin_console() -> HTMLResponse:
    """国内客服工作台：通讯录 + 聊天记录 + 人工回复。"""
    html_path = Path(__file__).with_name("static") / "admin.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/crm", response_class=HTMLResponse)
async def crm_console() -> HTMLResponse:
    """Independent sales CRM: pipeline, tasks and lead intelligence."""
    html_path = Path(__file__).with_name("static") / "crm.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/crm/home", response_class=HTMLResponse)
@app.get("/crm/tasks", response_class=HTMLResponse)
async def crm_dashboard() -> HTMLResponse:
    """CRM home and task workspace backed by live contact/task projections."""
    html_path = Path(__file__).with_name("static") / "crm-dashboard.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "admin_login.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/m/chat", response_class=HTMLResponse)
async def mobile_chat() -> HTMLResponse:
    """手机版客服聊天：仅保留客户列表 + 对话 + 发送 + 接管开关。"""
    html_path = Path(__file__).with_name("static") / "mobile-chat.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/admin/m", response_class=HTMLResponse)
async def admin_mobile_chat() -> HTMLResponse:
    """手机版客服聊天（/m/chat 的别名地址，/admin/m 更好记）。"""
    html_path = Path(__file__).with_name("static") / "mobile-chat.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/m/crm", response_class=HTMLResponse)
@app.get("/m/crm/home", response_class=HTMLResponse)
async def mobile_crm_home() -> HTMLResponse:
    """手机端 CRM 首页：会议/跟进与待办事项。"""
    html_path = Path(__file__).with_name("static") / "mobile-crm.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/teach", response_class=HTMLResponse)
async def teach_console() -> HTMLResponse:
    """教模型：编辑话术/知识库 + 话术训练。"""
    html_path = Path(__file__).with_name("static") / "teach.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/m/60", response_class=HTMLResponse)
@app.get("/models/60", response_class=HTMLResponse)
async def models_60_mobile() -> HTMLResponse:
    """手机友好：A60/B60/C60/D60 卡片索引 + 完整户型详情。"""
    html_path = Path(__file__).with_name("static") / "models60.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/m/70", response_class=HTMLResponse)
@app.get("/models/70", response_class=HTMLResponse)
async def models_70_mobile() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "models70.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/m/80", response_class=HTMLResponse)
@app.get("/models/80", response_class=HTMLResponse)
async def models_80_mobile() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "models80.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/m/box15", response_class=HTMLResponse)
@app.get("/models/box15", response_class=HTMLResponse)
@app.get("/m/15", response_class=HTMLResponse)
@app.get("/models/15", response_class=HTMLResponse)
async def models_box15_mobile() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "models-box15.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/m/box20", response_class=HTMLResponse)
@app.get("/models/box20", response_class=HTMLResponse)
@app.get("/m/20", response_class=HTMLResponse)
@app.get("/models/20", response_class=HTMLResponse)
async def models_box20_mobile() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "models-box20.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/m/box30", response_class=HTMLResponse)
@app.get("/models/box30", response_class=HTMLResponse)
@app.get("/m/30", response_class=HTMLResponse)
@app.get("/models/30", response_class=HTMLResponse)
async def models_box30_mobile() -> HTMLResponse:
    html_path = Path(__file__).with_name("static") / "models-box30.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


class AdminLoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/admin/session")
async def api_admin_session(request: Request) -> dict[str, Any]:
    return session_status(request)


@app.post("/api/admin/login")
async def api_admin_login(body: AdminLoginRequest) -> JSONResponse:
    if not verify_admin_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    settings = get_settings()
    token = create_session_token(body.username.strip())
    response = JSONResponse({"ok": True, "username": body.username.strip()})
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.admin_session_days * 86400,
        path="/",
    )
    return response


@app.post("/api/admin/logout")
async def api_admin_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return response


class TeachFileBody(BaseModel):
    content: str


class TeachPracticeRequest(BaseModel):
    message: str
    session_id: str | None = None
    locale: str = "en"


class TeachCaptureRequest(BaseModel):
    question: str
    answer: str


@app.get("/api/teach/files")
async def teach_list_files(_: str = Depends(require_admin)) -> dict[str, Any]:
    from grannyflat_bot.teach_store import list_teach_files

    return {"files": list_teach_files()}


@app.get("/api/teach/files/{kind}/{name}")
async def teach_get_file(
    kind: str, name: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    from grannyflat_bot.teach_store import read_teach_file

    try:
        return read_teach_file(kind, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.put("/api/teach/files/{kind}/{name}")
async def teach_put_file(
    kind: str, name: str, body: TeachFileBody, _: str = Depends(require_admin)
) -> dict[str, Any]:
    from grannyflat_bot.teach_store import write_teach_file

    try:
        data = write_teach_file(kind, name, body.content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError:
        raise HTTPException(
            status_code=500,
            detail=f"无法写入 {kind}/{name}：文件权限不足（需由运行服务的用户可写）",
        ) from None
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存失败：{exc}") from None
    reloaded = None
    if kind == "knowledge":
        reloaded = orchestrator.reload_knowledge()
    return {**data, "reloaded": reloaded}


@app.post("/api/teach/reload")
async def teach_reload(_: str = Depends(require_admin)) -> dict[str, Any]:
    return orchestrator.reload_knowledge()


def _prepare_user_text_for_orchestrator(original: str, llm: Any) -> tuple[str, str | None, str | None, bool]:
    """与演示对话一致：中文先译成英文再编排；返回 (process_text, user_zh, user_en, translated)."""
    from grannyflat_bot.llm.translate import (
        contains_chinese,
        en_to_zh,
        looks_primarily_english,
        zh_to_en,
    )

    text = (original or "").strip()
    if not text:
        return "", None, None, False
    if contains_chinese(text):
        user_en = zh_to_en(text, llm=llm)
        return user_en, text, user_en, user_en != text
    if looks_primarily_english(text):
        user_zh = en_to_zh(text, llm=llm)
        return text, user_zh, text, bool(user_zh and user_zh != text)
    return text, None, None, False


@app.post("/api/teach/practice")
async def teach_practice(
    body: TeachPracticeRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    session_id = (body.session_id or "").strip() or f"teach-{uuid.uuid4().hex[:10]}"
    if not session_id.startswith("teach-"):
        session_id = f"teach-{session_id}"
    locale = "en"
    process_text, user_zh, user_en, user_translated = _prepare_user_text_for_orchestrator(
        message, orchestrator.llm
    )
    result = orchestrator.handle(
        session_id,
        process_text,
        locale=locale,  # type: ignore[arg-type]
        channel="teach_practice",
    )
    if user_translated and user_zh:
        session = orchestrator.sessions.get(session_id)
        for msg in reversed(session.history):
            if msg.get("role") == "user":
                msg["content_zh"] = user_zh
                msg["content_en"] = user_en or msg.get("content")
                msg["translated"] = True
                orchestrator.sessions.save(session)
                break
    return {
        "session_id": session_id,
        "question": message,
        "question_en": user_en or process_text,
        "reply": result.reply,
        "questionnaire": result.questionnaire,
        "quote": result.quote.to_dict() if result.quote else None,
        "escalate_to_human": result.escalate_to_human,
        "user_translated": user_translated,
    }


@app.post("/api/teach/capture")
async def teach_capture(
    body: TeachCaptureRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    from grannyflat_bot.agent_learn import record_training_script

    question = (body.question or "").strip()
    answer = (body.answer or "").strip()
    if not question or not answer:
        raise HTTPException(status_code=400, detail="question and answer required")
    learned = record_training_script(
        question=question, answer=answer, llm=orchestrator.llm
    )
    if learned.get("saved"):
        orchestrator.reload_knowledge()
    return {"ok": True, "learned": learned}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    """Retained as a hard-disabled compatibility endpoint for old clients."""
    raise HTTPException(status_code=404, detail="Web demo has been retired")


@app.get("/api/contacts")
async def list_contacts(
    archived: str | None = None,
    sales_stage: str | None = None,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    # archived=1|true → 仅归档；archived=all → 全部；默认仅活跃
    flag: bool | None = False
    if archived in ("1", "true", "yes"):
        flag = True
    elif archived in ("all", "*"):
        flag = None
    return {
        "contacts": orchestrator.list_contacts(
            archived=flag, sales_stage=sales_stage
        )
    }


@app.get("/api/pipeline")
async def pipeline_summary(
    archived: bool = False, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.pipeline_summary(archived=archived)


@app.get("/api/tasks")
async def sales_tasks(_: str = Depends(require_admin)) -> dict[str, Any]:
    return orchestrator.list_sales_tasks()


@app.get("/api/contacts/categories")
async def contact_categories(_: str = Depends(require_admin)) -> dict[str, Any]:
    from grannyflat_bot.session.contacts import (
        CONTACT_TYPES,
        CUSTOMER_STATUSES,
        WORKFLOW_STATUSES,
        SALES_STAGES,
    )

    return {
        "contact_types": [
            {"code": code, "label": label} for code, label in CONTACT_TYPES.items()
        ],
        "customer_statuses": [
            {"code": code, "label": label} for code, label in CUSTOMER_STATUSES.items()
        ],
        "status_labels": [
            {"code": code, "label": label} for code, label in CUSTOMER_STATUSES.items()
        ],
        "workflow_statuses": [
            {"code": code, "label": label} for code, label in WORKFLOW_STATUSES.items()
        ],
        "sales_stages": [
            {"code": code, "label": label} for code, label in SALES_STAGES.items()
        ],
        # legacy
        "categories": [
            {"code": code, "label": label} for code, label in CONTACT_TYPES.items()
        ],
    }


@app.get("/api/contacts/{session_id}")
async def get_contact(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.get_contact(session_id)


@app.post("/api/contacts/{session_id}/read")
async def mark_contact_read(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.mark_contact_read(session_id)


@app.post("/api/contacts/{session_id}/ai-summary")
async def refresh_contact_ai_summary(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.refresh_contact_ai_summary(session_id)


@app.patch("/api/contacts/{session_id}")
async def patch_contact(
    session_id: str, body: ContactUpdate, _: str = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return orchestrator.update_contact(
            session_id,
            name=body.name,
            personal_name=body.personal_name,
            surname=body.surname,
            given=body.given,
            state=body.state,
            city=body.city,
            postcode=body.postcode,
            address=body.address,
            contact_type=body.contact_type or body.category,
            customer_status=body.customer_status,
            customer_status_auto=body.customer_status_auto,
            tags=body.tags,
            workflow_status=body.workflow_status,
            sales_stage=body.sales_stage,
            sales_stage_auto=body.sales_stage_auto,
            lead_temperature=body.lead_temperature,
            lead_temperature_auto=body.lead_temperature_auto,
            next_action=body.next_action,
            next_action_auto=body.next_action_auto,
            follow_up_due_at=body.follow_up_due_at,
            follow_up_due_auto=body.follow_up_due_auto,
            category=body.category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/contacts/{session_id}/archive")
async def archive_contact(
    session_id: str, body: ArchiveRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.set_archived(session_id, body.archived)


@app.delete("/api/contacts/{session_id}")
async def delete_contact(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.delete_contact(session_id)


@app.post("/api/contacts/{session_id}/human-mode")
async def set_human_mode(
    session_id: str, body: HumanModeRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.set_human_mode(session_id, body.enabled)


@app.post("/api/contacts/{session_id}/agent-message")
async def agent_message(
    session_id: str, body: AgentMessageRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return orchestrator.agent_send_message(
            session_id,
            body.message,
            agent_name=body.agent_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/translate/zh-en")
async def translate_zh_to_en(
    body: TranslateRequest, _: str = Depends(require_admin)
) -> dict[str, str]:
    """把移动端识别到的中文语音转成可确认的英文草稿。"""
    from grannyflat_bot.llm.translate import contains_chinese, zh_to_en

    original = (body.text or "").strip()
    if not original:
        raise HTTPException(status_code=400, detail="没有识别到中文内容")
    translated = zh_to_en(original, llm=orchestrator.llm).strip()
    if not translated or contains_chinese(translated):
        raise HTTPException(status_code=502, detail="中文翻译暂时不可用，请稍后重试")
    return {"original": original, "translation": translated}


@app.get("/api/session/{session_id}")
async def session_view(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    return orchestrator.get_session_view(session_id)


@app.post("/api/session/{session_id}/export")
async def export_session(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """手动导出当前会话（不受 recording 开关限制）。"""
    return orchestrator.export_session_files(session_id)


def _export_file(session_id: str, filename: str) -> Path:
    safe_id = session_id.replace("/", "_").replace("\\", "_")
    settings = get_settings()
    path = settings.session_export_dir / safe_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Export file not found")
    return path


@app.get("/api/session/{session_id}/download/profile.md")
async def download_profile(
    session_id: str, _: str = Depends(require_admin)
) -> FileResponse:
    return FileResponse(
        _export_file(session_id, "user_profile.md"),
        media_type="text/markdown",
        filename=f"{session_id}_profile.md",
    )


@app.get("/api/session/{session_id}/download/quote.xlsx")
async def download_quote_xlsx(
    session_id: str, _: str = Depends(require_admin)
) -> FileResponse:
    return FileResponse(
        _export_file(session_id, "quote_summary.xlsx"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{session_id}_quote.xlsx",
    )


@app.get("/api/session/{session_id}/download/transcript.json")
async def download_transcript(
    session_id: str, _: str = Depends(require_admin)
) -> FileResponse:
    return FileResponse(
        _export_file(session_id, "transcript.json"),
        media_type="application/json",
        filename=f"{session_id}_transcript.json",
    )


@app.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp(request: Request) -> Response:
    """Twilio 直连入口：收客户消息 → 编排器 → TwiML 自动回复（不经 Chatwoot）。"""
    form = dict(await request.form())
    form_str = {k: str(v) for k, v in form.items()}
    settings = get_settings()

    if not settings.twilio_configured:
        logger.error("Twilio webhook received but TWILIO_* not configured")
        return Response(
            content=twilio_channel.build_twiml(
                "Service is not configured. Please contact support."
            ),
            media_type="application/xml",
            status_code=503,
        )

    if not settings.twilio_whatsapp_enabled:
        logger.warning(
            "Twilio WhatsApp paused (TWILIO_WHATSAPP_ENABLED=0); ignoring inbound"
        )
        # 空 TwiML：不自动回复、不跑编排；网页演示不受影响
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    if settings.twilio_validate_webhook:
        sig = request.headers.get("X-Twilio-Signature", "")
        url = settings.twilio_inbound_webhook_url.strip() or str(request.url)
        if not twilio_channel.verify_signature(url, form_str, sig):
            return JSONResponse({"detail": "invalid signature"}, status_code=403)

    inbound = twilio_channel.parse_twilio_form(form_str)
    message_sid = form_str.get("MessageSid", "").strip()
    logger.info(
        "Twilio inbound sid=%s from=%s body_len=%d",
        message_sid or "missing",
        inbound.sender_id,
        len(inbound.text),
    )

    if not inbound.text:
        return Response(
            content=twilio_channel.build_twiml("Please send a text message."),
            media_type="application/xml",
        )

    if not message_sid:
        logger.warning(
            "Twilio inbound has no MessageSid; idempotency protection unavailable"
        )
    elif not orchestrator.sessions.claim_inbound_message(
        message_sid, inbound.session_id
    ):
        logger.info(
            "Ignoring duplicate Twilio inbound sid=%s session=%s",
            message_sid,
            inbound.session_id,
        )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    # WhatsApp 对客固定英文（whatsapp_reply_locale=bilingual 也不再生效）
    try:
        result = orchestrator.handle(
            inbound.session_id,
            inbound.text,
            locale="en",
            channel="whatsapp",
            whatsapp_phone=inbound.sender_id,
            whatsapp_profile_name=(inbound.raw or {}).get("ProfileName"),
        )
    except Exception:
        orchestrator.sessions.release_inbound_message(message_sid)
        logger.exception("Orchestrator failed for session %s", inbound.session_id)
        return Response(
            content=twilio_channel.build_twiml(
                "Sorry, something went wrong. Please try again in a moment."
            ),
            media_type="application/xml",
            status_code=500,
        )

    if result.bot_paused:
        # 人工接管：不向客户自动回复
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    tpl = (
        getattr(result, "layout_template", None)
        or getattr(result, "role_template", None)
        or getattr(result, "quote_template", None)
    )
    delay_s = _reply_delay_seconds(result.reply)
    if delay_s > _WEBHOOK_SYNC_DELAY_LIMIT_SECONDS:
        # 超出 webhook 安全窗口的拟人延时 → 存入待发队列，由后台 worker 到点发送。
        session = orchestrator.sessions.get(inbound.session_id)
        session.pending_bot_replies.append({
            "id": uuid.uuid4().hex,
            "text": result.reply,
            "template": tpl,
            "due_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=delay_s)
            ).isoformat(),
            "attempts": 0,
        })
        orchestrator.sessions.save(session, touch=False)
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    # 安全窗口内等待，模拟客服阅读和输入后同步返回 TwiML。
    await asyncio.sleep(delay_s)

    # 记录本轮文本回复对应的 history 时间戳（模板发送后 history 尾部会变成模板占位消息）
    session = orchestrator.sessions.get(inbound.session_id)
    reply_message_at = next(
        (
            m.get("at")
            for m in reversed(session.history)
            if m.get("role") == "assistant" and m.get("content") == result.reply
        ),
        None,
    )

    # 每轮最多发送一个模板；顺序由编排器控制：户型 → 身份选择 → 价格。
    if tpl:
        try:
            receipt_key = delivery_begin(inbound.session_id, message_at="")
            sent = await twilio_channel.send_content_template(
                to=inbound.sender_id,
                content_sid=tpl["sid"],
                content_variables=tpl.get("variables"),
                status_callback=DELIVERY_CALLBACK_URL + "?receipt=" + receipt_key,
            )
            delivery_update(receipt_key, (sent or {}).get("status") or "queued", (sent or {}).get("sid"))
            orchestrator.mark_template_sent(
                inbound.session_id,
                tpl,
                message_sid=(sent or {}).get("sid"),
            )
            fresh = orchestrator.sessions.get(inbound.session_id)
            delivery_fix_message_at(
                receipt_key, fresh.history[-1].get("at") if fresh.history else None
            )
            logger.info(
                "Twilio template sent: name=%s size=%s sid=%s to=%s msg=%s",
                tpl.get("name") or tpl.get("key"),
                tpl.get("size"),
                tpl.get("sid"),
                inbound.sender_id,
                (sent or {}).get("sid"),
            )
        except Exception:
            logger.exception(
                "Twilio template send failed: name=%s size=%s sid=%s to=%s",
                tpl.get("name") or tpl.get("key"),
                tpl.get("size"),
                tpl.get("sid"),
                inbound.sender_id,
            )

    # TwiML 回复：挂 statusCallback 让 Twilio 回报该消息的送达状态
    reply_status_callback = None
    if reply_message_at:
        receipt_key = delivery_begin(inbound.session_id, message_at=reply_message_at)
        reply_status_callback = DELIVERY_CALLBACK_URL + "?receipt=" + receipt_key

    return Response(
        content=twilio_channel.build_twiml(result.reply, status_callback=reply_status_callback),
        media_type="application/xml",
    )


install_delivery_callback(app)
