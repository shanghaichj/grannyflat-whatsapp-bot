from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grannyflat_bot.config import DATA_DIR
from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.quote.engine import QuoteResult
from grannyflat_bot.sales_intelligence import deterministic_summary, qualify_lead
from grannyflat_bot.session.contacts import (
    CONTACT_TYPES,
    CUSTOMER_STATUSES,
    WORKFLOW_STATUSES,
    SALES_STAGES,
    SALES_STAGE_NEXT_ACTIONS,
    extract_whatsapp_phone,
    infer_customer_status,
    infer_display_name,
    infer_sales_stage,
    infer_workflow_status,
    sync_address_from_requirements,
)


@dataclass
class Session:
    session_id: str
    requirements: RequirementState = field(default_factory=RequirementState)
    history: list[dict[str, Any]] = field(default_factory=list)
    last_quote: dict[str, Any] | None = None
    channel: str = "web"
    locale: str = "bilingual"
    exports: dict[str, Any] | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # 资料被「人工编辑」的时刻（改名字/城市/分类/标签/客户状态等）。
    # 只用于列表排序，绝不参与时间显示：
    #   - 显示用 updated_at（真实最后消息时间），否则会把 7 月的旧会话显示成「今天 00:05」
    #   - 排序用 max(updated_at, contact_edited_at)，让刚编辑过的客户也能排到前面
    contact_edited_at: str | None = None
    display_name: str | None = None
    name_manual: bool = False
    contact_surname: str | None = None
    contact_given: str | None = None
    state: str | None = None
    city: str | None = None
    postcode: str | None = None
    address: str | None = None
    contact_type: str = "unknown"
    tags: list[str] = field(default_factory=list)
    workflow_status: str = "new"
    sales_stage: str = "new_lead"
    sales_stage_manual: bool = False
    sales_stage_history: list[dict[str, Any]] = field(default_factory=list)
    next_action: str | None = None
    next_action_manual: bool = False
    follow_up_due_at: str | None = None
    follow_up_due_manual: bool = False
    ai_summary: str | None = None
    ai_summary_updated_at: str | None = None
    ai_summary_source: str = "rules"
    qualification_score: int = 0
    qualification_level: str = "early"
    qualification_missing: list[str] = field(default_factory=list)
    lead_temperature: str = "cold"
    lead_temperature_manual: bool = False
    lead_priority_reasons: list[str] = field(default_factory=list)
    customer_status: str = "new"
    customer_status_manual: bool = False
    human_mode: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    whatsapp_phone: str | None = None
    whatsapp_profile_name: str | None = None
    # 引导式三问：True=收齐前优先提问；客户说 skip 后变 False
    guided_intake: bool = True
    guided_quote_sent: bool = False  # 三问齐后是否已自动出过一次报价
    archived: bool = False  # 归档后默认不出现在通讯录主列表
    # 已成功提交给 Twilio 的自动模板，避免同一会话重复发送。
    sent_template_keys: list[str] = field(default_factory=list)
    # 机器人询问地址/州后，客户 10 分钟无回复时发送 60s 户型卡片。
    address_followup_due_at: str | None = None
    address_followup_attempts: int = 0
    # 只统计客户在上次打开会话后发来的消息；旧会话加载时视为已读。
    last_read_at: str | None = None
    pending_bot_replies: list[dict[str, Any]] = field(default_factory=list)

    def set_sales_stage(
        self,
        stage: str,
        *,
        source: str,
        note: str | None = None,
        manual: bool | None = None,
    ) -> bool:
        if stage not in SALES_STAGES:
            raise ValueError(f"invalid sales stage: {stage}")
        if stage == self.sales_stage:
            if manual is not None:
                self.sales_stage_manual = manual
            return False
        previous = self.sales_stage
        self.sales_stage = stage
        if manual is not None:
            self.sales_stage_manual = manual
        self.sales_stage_history.append(
            {
                "from": previous,
                "to": stage,
                "at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "note": note,
            }
        )
        self.sales_stage_history = self.sales_stage_history[-100:]
        if not self.next_action_manual:
            self.next_action = SALES_STAGE_NEXT_ACTIONS.get(stage)
        return True

    def unread_count(self) -> int:
        return sum(
            1 for item in self.history
            if item.get("role") == "user"
            and (self.last_read_at is None or str(item.get("at") or "") > self.last_read_at)
        )

    def mark_read(self) -> bool:
        # 历史导入的会话里 history 的顺序可能与时间顺序不一致（列表末条不一定是最新消息），
        # 因此这里必须按时间戳取最大值；否则 last_read_at 会停留在较旧的时间戳上，
        # 导致 unread_count() 永远大于 0、前端未读气泡无法清除（并触发每 5 秒重试的轮询）。
        latest = max(
            (str(item.get("at") or "") for item in self.history
             if item.get("role") == "user"),
            default=None,
        )
        if not latest or latest == self.last_read_at:
            return False
        self.last_read_at = latest
        return True

    def add_message(
        self,
        role: str,
        content: str,
        *,
        max_history: int = 500,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "role": role,
            "content": content,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)
        self.history.append(entry)
        if len(self.history) > max_history:
            self.history = self.history[-max_history:]
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def sync_contact_fields(self) -> bool:
        """Refresh contact fields. Returns True when any tracked value changed.

        注意：这里**绝对不能**改动 updated_at / contact_edited_at。
        本方法会被 list_contacts() 对每个会话无条件调用（前端每 5 秒轮询一次列表），
        一旦在这里写时间戳，全库会话的 updated_at 就会被批量刷成"当前时间"：
          - 7 月的旧会话在列表里显示成「今天 00:05」（只显示时间、不显示日期）
          - 列表排序被彻底打乱，旧会话全部浮到顶部
        历史教训：2026-09-24 一次批量污染影响了 752 个会话中的 430 个。
        """
        before = (
            self.display_name,
            self.state,
            self.city,
            self.postcode,
            self.address,
            self.contact_type,
            self.workflow_status,
            self.sales_stage,
            self.next_action,
            self.ai_summary,
            self.qualification_score,
            self.qualification_level,
            tuple(self.qualification_missing),
            self.lead_temperature,
            tuple(self.lead_priority_reasons),
            self.customer_status,
            self.whatsapp_phone,
        )

        sync_address_from_requirements(self)
        if not self.whatsapp_phone:
            self.whatsapp_phone = extract_whatsapp_phone(self.session_id)

        manual_name = self.display_name if self.name_manual else None
        self.display_name = infer_display_name(
            self.requirements,
            session_id=self.session_id,
            manual_name=manual_name,
            whatsapp_profile_name=self.whatsapp_profile_name,
            name_manual=self.name_manual,
            city=self.city,
            state=self.state,
            postcode=self.postcode,
            address=self.address,
            whatsapp_phone=self.whatsapp_phone,
        )

        if self.workflow_status == "follow_up":
            pass
        elif self.human_mode:
            self.workflow_status = "human"
        else:
            self.workflow_status = infer_workflow_status(
                human_mode=False,
                escalated=self.escalated,
                has_quote=bool(self.last_quote),
                questionnaire=self.requirements.as_questionnaire(),
                current=self.workflow_status,
            )

        self.customer_status = infer_customer_status(
            history=self.history,
            escalated=self.escalated,
            workflow_status=self.workflow_status,
            human_mode=self.human_mode,
            current=self.customer_status,
            manual=self.customer_status_manual,
        )

        if not self.sales_stage_manual:
            inferred_stage = infer_sales_stage(
                current=self.sales_stage,
                history=self.history,
                requirements=self.requirements,
                last_quote=self.last_quote,
                sent_template_keys=self.sent_template_keys,
            )
            self.set_sales_stage(inferred_stage, source="system", manual=False)
        if not self.next_action_manual and self.next_action is None:
            self.next_action = SALES_STAGE_NEXT_ACTIONS.get(self.sales_stage)

        qualification = qualify_lead(
            self.requirements,
            history=self.history,
            sales_stage=self.sales_stage,
            whatsapp_phone=self.whatsapp_phone,
            contact_type=self.contact_type,
            sent_template_keys=self.sent_template_keys,
        )
        self.qualification_score = qualification.score
        self.qualification_level = qualification.level
        self.qualification_missing = qualification.missing
        if not self.lead_temperature_manual:
            self.lead_temperature = qualification.temperature
        self.lead_priority_reasons = qualification.reasons
        if not self.ai_summary or self.ai_summary_source == "rules":
            summary = deterministic_summary(
                self.requirements,
                history=self.history,
                sales_stage=self.sales_stage,
            )
            if summary != self.ai_summary:
                self.ai_summary = summary
                self.ai_summary_updated_at = datetime.now(timezone.utc).isoformat()
                self.ai_summary_source = "rules"

        after = (
            self.display_name,
            self.state,
            self.city,
            self.postcode,
            self.address,
            self.contact_type,
            self.workflow_status,
            self.sales_stage,
            self.next_action,
            self.ai_summary,
            self.qualification_score,
            self.qualification_level,
            tuple(self.qualification_missing),
            self.lead_temperature,
            tuple(self.lead_priority_reasons),
            self.customer_status,
            self.whatsapp_phone,
        )
        return before != after

    def last_message_entry(self) -> dict[str, Any] | None:
        """取时间上最新的那条「真实对话」消息。

        不能直接取 history[-1]：历史导入的会话里 history 顺序与时间顺序不一致，
        末条未必是最新消息。忽略 role == "system" 的记账记录
        （如导入时批量写入的「[图片] 已保存: ...」，时间戳是导入时刻）。
        """
        best: dict[str, Any] | None = None
        best_at = ""
        for item in self.history:
            if str(item.get("role") or "") == "system":
                continue
            at = str(item.get("at") or "")
            if not at:
                continue
            if best is None or at > best_at:
                best, best_at = item, at
        if best is None and self.history:
            # 全是 system 记录时退回末条，保证预览不为空
            best = self.history[-1]
        return best

    def last_message_preview(self) -> str:
        entry = self.last_message_entry()
        if not entry:
            return ""
        return str(entry.get("content", ""))[:120]

    def has_real_message(self) -> bool:
        """是否存在真正的客户对话（排除 system 记账）。

        批量导入的联系人可能只有图片保存记录、没有任何对话内容，
        列表排序时应沉到底部，避免顶部堆一批空白联系人。
        """
        return any(
            str(item.get("role") or "") != "system" for item in self.history
        )

    def real_message_count(self) -> int:
        """真实对话条数，排除 system 记账。

        前端聊天窗口已过滤 system 记录，这里保持一致，
        否则会出现「列表显示 116 条、点进去只有 110 条」的落差。
        """
        return sum(
            1 for item in self.history
            if str(item.get("role") or "") != "system"
        )

    def last_activity_at(self) -> str:
        """时间上最后一次真实对话的时间（用于自愈/校对 updated_at）。"""
        entry = self.last_message_entry()
        if entry:
            return str(entry.get("at") or "") or self.created_at
        return self.created_at

    def sort_key(self) -> str:
        """列表排序键：最后消息时间 与 资料编辑时间 取较新者。

        这样既有新消息的会话正常置顶，刚手动编辑过的客户也会排到前面，
        同时 updated_at 本身保持真实含义（用于显示）。
        """
        return max(self.updated_at or "", self.contact_edited_at or "")

    def mark_contact_edited(self) -> None:
        """人工编辑资料后调用：只推进排序键，不动 updated_at。"""
        self.contact_edited_at = datetime.now(timezone.utc).isoformat()


def _load_contact_type(data: dict[str, Any]) -> str:
    ct = data.get("contact_type")
    if isinstance(ct, str) and ct in CONTACT_TYPES:
        return ct
    cat = data.get("category")
    if isinstance(cat, str) and cat in CONTACT_TYPES:
        return cat
    return "unknown"


def _load_workflow_status(data: dict[str, Any]) -> str:
    ws = data.get("workflow_status")
    if isinstance(ws, str) and ws in WORKFLOW_STATUSES:
        return ws
    cat = data.get("category")
    if isinstance(cat, str) and cat in WORKFLOW_STATUSES:
        return cat
    return "new"


def _load_customer_status(data: dict[str, Any]) -> str:
    cs = data.get("customer_status")
    if isinstance(cs, str) and cs in CUSTOMER_STATUSES:
        return cs
    return "new"


def _load_sales_stage(data: dict[str, Any]) -> str:
    stage = data.get("sales_stage")
    if isinstance(stage, str) and stage in SALES_STAGES:
        return stage
    legacy = str(data.get("workflow_status") or "new")
    return {
        "new": "new_lead",
        "quoting": "qualified",
        "quoted": "quotation",
        "escalated": "contacted",
        "human": "contacted",
        "follow_up": "contacted",
    }.get(legacy, "new_lead")


class SessionStore:
    """SQLite session store (memory fallback on failure)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, Session] = {}
        self._processed_inbound_message_sids: set[str] = set()
        self.db_path = db_path or (DATA_DIR / "sessions" / "sessions.sqlite3")
        self._conn: sqlite3.Connection | None = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_inbound_messages (
                    message_sid TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
        except Exception:
            self._conn = None

    def get(self, session_id: str) -> Session:
        with self._lock:
            if session_id in self._memory:
                return self._memory[session_id]
            session = self._load(session_id) or Session(session_id=session_id)
            self._memory[session_id] = session
            return session

    def save(self, session: Session, *, touch: bool = False) -> None:
        """写回会话。

        touch 默认 **False**：`updated_at` 的语义是「最后一条消息的时间」，
        只应由 `add_message()` 写入。过去这里默认 True，导致任何一次保存
        （打开会话详情、标记已读、同步资料、改归档……）都会把 updated_at 刷成
        当前时间 —— 结果是「点开一个 7 月的客户，它就跳到列表顶部并显示成今天」。
        与 sync_contact_fields() 那次批量污染是同一类问题。

        需要让某类操作把客户顶到列表前面时，请用 mark_contact_edited()
        推进 contact_edited_at（只影响排序，不影响显示时间）。
        """
        with self._lock:
            if touch:
                session.updated_at = datetime.now(timezone.utc).isoformat()
            self._memory[session.session_id] = session
            self._persist(session)

    def claim_inbound_message(self, message_sid: str, session_id: str) -> bool:
        """Atomically claim a Twilio inbound message for processing.

        Twilio may retry the same webhook.  A durable primary key makes the first
        delivery win across workers and service restarts; later deliveries must
        be acknowledged without running the conversation pipeline again.

        Missing SIDs are deliberately not claimed so non-Twilio callers and old
        fixtures remain usable.  The webhook logs that degraded path.
        """
        sid = message_sid.strip()
        if not sid:
            return True
        received_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_inbound_messages
                        (message_sid, session_id, received_at)
                    VALUES (?, ?, ?)
                    """,
                    (sid, session_id, received_at),
                )
                self._conn.commit()
                return cursor.rowcount == 1
            if sid in self._processed_inbound_message_sids:
                return False
            self._processed_inbound_message_sids.add(sid)
            return True

    def release_inbound_message(self, message_sid: str) -> None:
        """Release a failed claim so a later Twilio retry can be processed."""
        sid = message_sid.strip()
        if not sid:
            return
        with self._lock:
            self._processed_inbound_message_sids.discard(sid)
            if self._conn:
                self._conn.execute(
                    "DELETE FROM processed_inbound_messages WHERE message_sid = ?",
                    (sid,),
                )
                self._conn.commit()

    def delete(self, session_id: str) -> bool:
        """永久删除联系人会话。"""
        with self._lock:
            existed = session_id in self._memory
            self._memory.pop(session_id, None)
            if self._conn:
                cur = self._conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                self._conn.commit()
                return existed or cur.rowcount > 0
            return existed

    def set_quote(self, session: Session, quote: QuoteResult) -> None:
        session.last_quote = quote.to_dict()
        session.sync_contact_fields()
        self.save(session)

    def list_all(self) -> list[Session]:
        """List all sessions sorted by updated_at descending."""
        with self._lock:
            seen: set[str] = set()
            sessions: list[Session] = []

            if self._conn:
                rows = self._conn.execute(
                    "SELECT session_id, payload FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
                for session_id, payload in rows:
                    if session_id in self._memory:
                        sessions.append(self._memory[session_id])
                    else:
                        data = json.loads(payload)
                        sessions.append(self._session_from_payload(session_id, data))
                        self._memory[session_id] = sessions[-1]
                    seen.add(session_id)

            for sid, session in self._memory.items():
                if sid not in seen:
                    sessions.append(session)

            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            return sessions

    def _load(self, session_id: str) -> Session | None:
        if not self._conn:
            return None
        row = self._conn.execute(
            "SELECT payload FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return self._session_from_payload(session_id, json.loads(row[0]))

    def _session_from_payload(self, session_id: str, data: dict[str, Any]) -> Session:
        history = list(data.get("history") or [])
        # 缺 at 的消息用 created_at 兜底，**绝不能**用 updated_at：
        # updated_at 是「最后活动时间」，一旦它被批量污染，这个假值会被填进 history
        # 并随 save 落库固化，之后再也恢复不回来。
        # 2026-09-24 事故：630 条消息因此被写入带微秒的假时间戳，波及 411 个会话。
        fallback_at = (
            data.get("created_at") or datetime.now(timezone.utc).isoformat()
        )
        for item in history:
            if "at" not in item:
                item["at"] = fallback_at
        return Session(
            session_id=session_id,
            requirements=RequirementState.model_validate(data.get("requirements") or {}),
            history=history,
            last_quote=data.get("last_quote"),
            channel=data.get("channel") or "web",
            locale=data.get("locale") or "bilingual",
            exports=data.get("exports"),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            updated_at=data.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            contact_edited_at=data.get("contact_edited_at"),
            display_name=data.get("display_name"),
            name_manual=bool(data.get("name_manual")),
            contact_surname=data.get("contact_surname"),
            contact_given=data.get("contact_given"),
            state=data.get("state"),
            city=data.get("city"),
            postcode=data.get("postcode"),
            address=data.get("address"),
            contact_type=_load_contact_type(data),
            tags=list(data.get("tags") or []),
            workflow_status=_load_workflow_status(data),
            sales_stage=_load_sales_stage(data),
            sales_stage_manual=bool(data.get("sales_stage_manual", False)),
            sales_stage_history=list(data.get("sales_stage_history") or []),
            next_action=data.get("next_action"),
            next_action_manual=bool(data.get("next_action_manual", False)),
            follow_up_due_at=data.get("follow_up_due_at"),
            follow_up_due_manual=bool(data.get("follow_up_due_manual", False)),
            ai_summary=data.get("ai_summary"),
            ai_summary_updated_at=data.get("ai_summary_updated_at"),
            ai_summary_source=str(data.get("ai_summary_source") or "rules"),
            qualification_score=int(data.get("qualification_score") or 0),
            qualification_level=str(data.get("qualification_level") or "early"),
            qualification_missing=list(data.get("qualification_missing") or []),
            lead_temperature=str(data.get("lead_temperature") or "cold"),
            lead_temperature_manual=bool(data.get("lead_temperature_manual", False)),
            lead_priority_reasons=list(data.get("lead_priority_reasons") or []),
            customer_status=_load_customer_status(data),
            customer_status_manual=bool(data.get("customer_status_manual")),
            human_mode=bool(data.get("human_mode")),
            escalated=bool(data.get("escalated")),
            escalation_reason=data.get("escalation_reason"),
            whatsapp_phone=data.get("whatsapp_phone"),
            whatsapp_profile_name=data.get("whatsapp_profile_name"),
            guided_intake=bool(data.get("guided_intake", True)),
            guided_quote_sent=bool(data.get("guided_quote_sent", False)),
            archived=bool(data.get("archived", False)),
            sent_template_keys=list(data.get("sent_template_keys") or []),
            address_followup_due_at=data.get("address_followup_due_at"),
            address_followup_attempts=int(data.get("address_followup_attempts") or 0),
            last_read_at=(
                data.get("last_read_at")
                if "last_read_at" in data
                else next(
                    (str(item.get("at") or "") for item in reversed(history)
                     if item.get("role") == "user"),
                    None,
                )
            ),
            pending_bot_replies=list(data.get("pending_bot_replies") or []),
        )

    def _persist(self, session: Session) -> None:
        if not self._conn:
            return
        payload = {
            "requirements": session.requirements.model_dump(mode="json"),
            "history": session.history,
            "last_quote": session.last_quote,
            "channel": session.channel,
            "locale": session.locale,
            "exports": session.exports,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "contact_edited_at": session.contact_edited_at,
            "display_name": session.display_name,
            "name_manual": session.name_manual,
            "contact_surname": session.contact_surname,
            "contact_given": session.contact_given,
            "state": session.state,
            "city": session.city,
            "postcode": session.postcode,
            "address": session.address,
            "contact_type": session.contact_type,
            "tags": session.tags,
            "workflow_status": session.workflow_status,
            "sales_stage": session.sales_stage,
            "sales_stage_manual": session.sales_stage_manual,
            "sales_stage_history": session.sales_stage_history,
            "next_action": session.next_action,
            "next_action_manual": session.next_action_manual,
            "follow_up_due_at": session.follow_up_due_at,
            "follow_up_due_manual": session.follow_up_due_manual,
            "ai_summary": session.ai_summary,
            "ai_summary_updated_at": session.ai_summary_updated_at,
            "ai_summary_source": session.ai_summary_source,
            "qualification_score": session.qualification_score,
            "qualification_level": session.qualification_level,
            "qualification_missing": session.qualification_missing,
            "lead_temperature": session.lead_temperature,
            "lead_temperature_manual": session.lead_temperature_manual,
            "lead_priority_reasons": session.lead_priority_reasons,
            "customer_status": session.customer_status,
            "customer_status_manual": session.customer_status_manual,
            "human_mode": session.human_mode,
            "escalated": session.escalated,
            "escalation_reason": session.escalation_reason,
            "whatsapp_phone": session.whatsapp_phone,
            "whatsapp_profile_name": session.whatsapp_profile_name,
            "guided_intake": session.guided_intake,
            "guided_quote_sent": session.guided_quote_sent,
            "archived": session.archived,
            "sent_template_keys": session.sent_template_keys,
            "address_followup_due_at": session.address_followup_due_at,
            "address_followup_attempts": session.address_followup_attempts,
            "last_read_at": session.last_read_at,
            "pending_bot_replies": session.pending_bot_replies,
        }
        self._conn.execute(
            """
            INSERT INTO sessions (session_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (session.session_id, json.dumps(payload), session.updated_at),
        )
        self._conn.commit()
