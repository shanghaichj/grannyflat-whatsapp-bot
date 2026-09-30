"""Admin workspace for the independently stored legacy WhatsApp number."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import re
import sqlite3
from pathlib import Path
import threading
from typing import Any, Iterator, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from grannyflat_bot.admin_auth import require_admin
from grannyflat_bot.config import DATA_DIR
from grannyflat_bot.legacy_analysis import (
    MAX_SCOPE_IDS,
    TAG_PRESETS,
    analyze_legacy_history,
    distinct_tags,
)
from grannyflat_bot.llm.client import LLMClient
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.session.contacts import (
    CONTACT_TYPES,
    CUSTOMER_STATUSES,
    WORKFLOW_STATUSES,
    SALES_STAGES,
)
from grannyflat_bot.session.store import SessionStore


LEGACY_SESSION_DB = DATA_DIR / "legacy_number" / "sessions.sqlite3"
LEGACY_NOTES_DB = DATA_DIR / "legacy_number" / "contact_notes.sqlite3"
MAX_CONTACT_NOTE_LENGTH = 2000
router = APIRouter()
logger = logging.getLogger(__name__)

# Reuse the tested contact-management methods without constructing another LLM stack.
# This object can only see the old number's physically separate database.
legacy_admin = ChatOrchestrator.__new__(ChatOrchestrator)
legacy_admin.sessions = SessionStore(LEGACY_SESSION_DB)
# AI 摘要/历史分析共用一个 LLM 客户端；MOCK_LLM=1 时 live=False，自动退回确定性摘要。
legacy_llm = LLMClient()
legacy_admin.llm = legacy_llm


class LegacyContactNoteStore:
    """Small sidecar store used only by the independently stored old number.

    Keeping staff notes outside ``Session`` prevents them from entering the active
    number's database, the LLM requirements payload or any WhatsApp message flow.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_notes (
                    session_id TEXT PRIMARY KEY,
                    note TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self) -> dict[str, str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT session_id, note FROM contact_notes").fetchall()
        return {str(session_id): str(note) for session_id, note in rows}

    def get(self, session_id: str) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT note FROM contact_notes WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def set(self, session_id: str, note: str) -> str:
        cleaned = note.strip()
        with self._lock, self._connect() as conn:
            if cleaned:
                conn.execute(
                    """
                    INSERT INTO contact_notes (session_id, note, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        note = excluded.note,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, cleaned, _utc_now()),
                )
            else:
                conn.execute(
                    "DELETE FROM contact_notes WHERE session_id = ?",
                    (session_id,),
                )
        return cleaned

    def delete(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM contact_notes WHERE session_id = ?",
                (session_id,),
            )


legacy_notes = LegacyContactNoteStore(LEGACY_NOTES_DB)

TEMPLATE_RE = re.compile(r"^/\s*([A-Za-z0-9]+)\s*$")
TEMPLATE_KEYS = {
    "15s", "20s", "30s", "60s", "70s", "80s",
    "60price", "70price", "introduce", "shiping", "address",
}


class ContactUpdate(BaseModel):
    name: str | None = None
    personal_name: str | None = None
    surname: str | None = None
    given: str | None = None
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
    category: str | None = None
    note: str | None = Field(default=None, max_length=MAX_CONTACT_NOTE_LENGTH)


class ArchiveRequest(BaseModel):
    archived: bool = True


class HumanModeRequest(BaseModel):
    enabled: bool


class AgentMessageRequest(BaseModel):
    message: str
    agent_name: str = "客服"


class LegacyAnalyzeRequest(BaseModel):
    """固定 AI 历史分析请求：不再需要自然语言指令。"""

    scope: Literal["all", "filtered", "selected"] = "all"
    session_ids: list[str] = Field(default_factory=list, max_length=MAX_SCOPE_IDS)

    @field_validator("session_ids")
    @classmethod
    def clean_session_ids(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            session_id = str(value).strip()
            if session_id and session_id not in seen:
                seen.add(session_id)
                unique.append(session_id)
        return unique


_MAX_ANALYSIS_JOBS = 20
_analysis_jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
_analysis_jobs_guard = asyncio.Lock()
_analysis_task: asyncio.Task[Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _with_legacy_note(
    contact: dict[str, Any], *, notes: dict[str, str] | None = None
) -> dict[str, Any]:
    """Return a copy with the staff-only old-number note attached."""
    session_id = str(contact.get("session_id") or "")
    note = notes.get(session_id, "") if notes is not None else legacy_notes.get(session_id)
    return {**contact, "note": note}


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "job_id": job["job_id"],
        "status": job["status"],
        "processed": int(job.get("processed") or 0),
        "total": int(job.get("total") or 0),
    }
    if job.get("result"):
        view.update(job["result"])
    if job.get("message"):
        view["message"] = job["message"]
    return view


def _trim_jobs() -> None:
    while len(_analysis_jobs) >= _MAX_ANALYSIS_JOBS:
        removable = next(
            (
                job_id for job_id, job in _analysis_jobs.items()
                if job.get("status") in {"completed", "failed"}
            ),
            None,
        )
        if removable is None:
            break
        _analysis_jobs.pop(removable, None)


async def _run_analysis_job(job_id: str, body: LegacyAnalyzeRequest) -> None:
    global _analysis_task
    loop = asyncio.get_running_loop()

    def _progress(done: int, total: int) -> None:
        # 工作线程中安全地把进度写回任务记录（事件循环线程内执行）。
        def _update() -> None:
            job = _analysis_jobs.get(job_id)
            if job is not None:
                job["processed"] = done
                job["total"] = total

        loop.call_soon_threadsafe(_update)

    async with _analysis_jobs_guard:
        job = _analysis_jobs.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = _utc_now()
        job["processed"] = 0
        job["total"] = 0
    try:
        result = await asyncio.to_thread(
            analyze_legacy_history,
            legacy_admin.sessions,
            scope=body.scope,
            session_ids=tuple(body.session_ids),
            llm=legacy_llm,
            progress=_progress,
        )
    except asyncio.CancelledError:
        async with _analysis_jobs_guard:
            job = _analysis_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="failed",
                    finished_at=_utc_now(),
                    message="分析任务已停止，请重新执行。",
                )
        raise
    except Exception:
        # The public job record deliberately contains no instruction, contact or
        # message content. Detailed diagnostics stay in the protected server log.
        logger.exception("Legacy analysis job %s failed", job_id)
        async with _analysis_jobs_guard:
            job = _analysis_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="failed",
                    finished_at=_utc_now(),
                    message="分析失败，请稍后重试或检查服务器日志。",
                )
    else:
        async with _analysis_jobs_guard:
            job = _analysis_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="completed",
                    finished_at=_utc_now(),
                    result=result.as_dict(),
                    message=result.message,
                )
    finally:
        async with _analysis_jobs_guard:
            _analysis_task = None


def _existing(session_id: str):
    if not any(s.session_id == session_id for s in legacy_admin.sessions.list_all()):
        raise HTTPException(status_code=404, detail="Legacy contact not found")
    return legacy_admin.sessions.get(session_id)


@router.get("/admin/old-number", response_class=HTMLResponse)
async def old_number_admin() -> HTMLResponse:
    path = Path(__file__).with_name("static") / "legacy-admin.html"
    return HTMLResponse(
        content=path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/api/legacy/contacts")
async def list_contacts(
    archived: str | None = None, _: str = Depends(require_admin)
) -> dict[str, Any]:
    flag: bool | None = False
    if archived in ("1", "true", "yes"):
        flag = True
    elif archived in ("all", "*"):
        flag = None
    contacts = legacy_admin.list_contacts(archived=flag)
    notes = legacy_notes.all()
    return {"contacts": [_with_legacy_note(contact, notes=notes) for contact in contacts]}


@router.get("/api/legacy/contacts/categories")
async def contact_categories(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {
        "contact_types": [{"code": c, "label": l} for c, l in CONTACT_TYPES.items()],
        "customer_statuses": [{"code": c, "label": l} for c, l in CUSTOMER_STATUSES.items()],
        "status_labels": [{"code": c, "label": l} for c, l in CUSTOMER_STATUSES.items()],
        "workflow_statuses": [{"code": c, "label": l} for c, l in WORKFLOW_STATUSES.items()],
        "categories": [{"code": c, "label": l} for c, l in CONTACT_TYPES.items()],
        "sales_stages": [
            {"code": code, "label": label} for code, label in SALES_STAGES.items()
        ],
    }


@router.get("/api/legacy/tags")
async def legacy_tags(_: str = Depends(require_admin)) -> dict[str, Any]:
    tags = await asyncio.to_thread(distinct_tags, legacy_admin.sessions)
    return {"presets": list(TAG_PRESETS), "tags": tags}


@router.post("/api/legacy/analyze", status_code=202)
async def start_legacy_analysis(
    body: LegacyAnalyzeRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """Queue one non-sending analysis against the isolated legacy store."""
    global _analysis_task
    if body.scope in {"filtered", "selected"} and not body.session_ids:
        raise HTTPException(status_code=400, detail="当前分析范围没有联系人")

    async with _analysis_jobs_guard:
        active = next(
            (
                job for job in _analysis_jobs.values()
                if job.get("status") in {"queued", "running"}
            ),
            None,
        )
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "已有老号码分析任务正在运行",
                    "job_id": active["job_id"],
                },
            )
        _trim_jobs()
        job_id = uuid4().hex
        _analysis_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _utc_now(),
            "result": None,
        }
        _analysis_task = asyncio.create_task(_run_analysis_job(job_id, body))
        return {"job_id": job_id, "status": "queued"}


@router.get("/api/legacy/analyze/{job_id}")
async def get_legacy_analysis_job(
    job_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    async with _analysis_jobs_guard:
        job = _analysis_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="分析任务不存在或已过期")
        return _job_view(job)


@router.post("/api/legacy/contacts/{session_id}/ai-summary")
async def refresh_legacy_ai_summary(
    session_id: str, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """为单个老号码联系人重新生成 AI 销售摘要（对齐新号码客服页面）。"""
    _existing(session_id)
    return _with_legacy_note(legacy_admin.refresh_contact_ai_summary(session_id))


@router.get("/api/legacy/contacts/{session_id}")
async def get_contact(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    _existing(session_id)
    return _with_legacy_note(legacy_admin.get_contact(session_id))


@router.post("/api/legacy/contacts/{session_id}/read")
async def mark_read(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    _existing(session_id)
    return legacy_admin.mark_contact_read(session_id)


@router.patch("/api/legacy/contacts/{session_id}")
async def patch_contact(
    session_id: str, body: ContactUpdate, _: str = Depends(require_admin)
) -> dict[str, Any]:
    session = _existing(session_id)
    contact_type = body.contact_type or body.category
    tags = list(body.tags) if body.tags is not None else list(session.tags or [])
    contact = legacy_admin.update_contact(
        session_id,
        name=body.name,
        personal_name=body.personal_name,
        surname=body.surname,
        given=body.given,
        state=body.state,
        city=body.city,
        postcode=body.postcode,
        address=body.address,
        contact_type=contact_type,
        customer_status=body.customer_status,
        customer_status_auto=body.customer_status_auto,
        tags=tags if body.tags is not None else None,
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
    if body.note is not None:
        legacy_notes.set(session_id, body.note)
    return _with_legacy_note(contact)


@router.post("/api/legacy/contacts/{session_id}/archive")
async def archive_contact(
    session_id: str, body: ArchiveRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    _existing(session_id)
    return _with_legacy_note(legacy_admin.set_archived(session_id, body.archived))


@router.delete("/api/legacy/contacts/{session_id}")
async def delete_contact(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    _existing(session_id)
    result = legacy_admin.delete_contact(session_id)
    if result.get("ok"):
        legacy_notes.delete(session_id)
    return result


@router.post("/api/legacy/contacts/{session_id}/human-mode")
async def set_human_mode(
    session_id: str, body: HumanModeRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    _existing(session_id)
    return _with_legacy_note(legacy_admin.set_human_mode(session_id, body.enabled))


@router.post("/api/legacy/contacts/{session_id}/agent-message")
async def agent_message(
    session_id: str, body: AgentMessageRequest, _: str = Depends(require_admin)
) -> dict[str, Any]:
    session = _existing(session_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message cannot be empty")
    match = TEMPLATE_RE.match(text)
    template = match.group(1).lower() if match and match.group(1).lower() in TEMPLATE_KEYS else None
    content = f"[WhatsApp template recorded: {template}]" if template else text
    session.add_message(
        "agent", content,
        extra={"agent_name": body.agent_name, "template_name": template, "recorded_only": True},
    )
    session.human_mode = True
    session.workflow_status = "human"
    legacy_admin.sessions.save(session)
    return {
        **_with_legacy_note(legacy_admin.get_contact(session_id)),
        "delivery": {
            "channel": "legacy_archive",
            "type": "template" if template else "agent_note",
            "template_name": template,
            "sent": False,
            "note": "老号码未连接发送通道，内容已记录到老号码档案",
        },
        "learned": {"saved": False, "skipped": "legacy_archive"},
    }


@router.get("/api/legacy/session/{session_id}")
async def session_view(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    session = _existing(session_id)
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "channel": session.channel,
        "locale": session.locale,
        "questionnaire": session.requirements.as_questionnaire(),
        "requirements": session.requirements.model_dump(mode="json"),
        "last_quote": session.last_quote,
        "history": session.history,
        "exports": session.exports,
        "note": legacy_notes.get(session_id),
    }
