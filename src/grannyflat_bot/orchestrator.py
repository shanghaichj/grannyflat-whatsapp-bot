"""对话编排：检索知识 → 抽取问卷 → 顾问口吻回复（示意价来自补充话术，不用规则引擎对客）。

编排层把渠道消息、会话状态、知识检索与 LLM 抽取串起来；对客价格只引用补充话术区间。
对客文案默认英文；本模块注释用中文，方便国内同事排查转人工 / 报价逻辑。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from grannyflat_bot.config import get_settings
from grannyflat_bot.delivery_tracking import history_with_receipts, send_tracked
from grannyflat_bot.export import SessionExporter, get_recording_state
from grannyflat_bot.intake.extractor import RequirementExtractor
from grannyflat_bot.intake.guided_flow import (
    format_guided_question,
    format_intake_complete,
    next_missing_step,
    user_wants_skip_guided,
)
from grannyflat_bot.intake.schema import REQUIRED_FOR_QUOTE, RequirementState
from grannyflat_bot.knowledge.retriever import KnowledgeRetriever
from grannyflat_bot.llm.client import LLMClient
from grannyflat_bot.llm.prompts import ReplyLocale
from grannyflat_bot.quote.engine import QuoteEngine, QuoteResult
from grannyflat_bot.sales_intelligence import (
    LEAD_TEMPERATURE_LABELS,
    QUALIFICATION_LABELS,
    default_next_action,
    deterministic_summary,
    follow_up_datetime,
    generate_ai_summary,
    safe_internal_text,
)
from grannyflat_bot.sales_tasks import build_sales_tasks, task_summary
from grannyflat_bot.session.contacts import (
    CONTACT_TYPES,
    CUSTOMER_STATUSES,
    WORKFLOW_STATUSES,
    SALES_STAGES,
    contact_type_label,
    customer_status_label,
    extract_location_from_text,
    extract_postcode_from_text,
    extract_unit_count_from_text,
    extract_whatsapp_phone,
    format_location_summary,
    infer_contact_type,
    infer_display_name,
    normalize_address,
    resolve_personal_name,
    sales_stage_label,
    workflow_status_label,
)
from grannyflat_bot.session.store import Session, SessionStore

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """单轮处理结果（reply 对客英文；escalate_* 供内部工作台使用）。"""

    reply: str
    requirements: RequirementState
    quote: QuoteResult | None = None
    escalate_to_human: bool = False  # 是否申请人工介入
    escalation_reason: str | None = None
    questionnaire: dict[str, Any] = field(default_factory=dict)
    exports: dict[str, Any] | None = None
    bot_paused: bool = False  # 人工接管中，未自动回复
    quote_template: dict[str, Any] | None = None  # 报价官方模板 {sid,size,variables}（仅 WhatsApp 渠道发送）
    layout_template: dict[str, Any] | None = None  # 户型官方模板；每轮最多发送一个面积段
    role_template: dict[str, Any] | None = None  # 客户身份选择器；首次有效答复后的空闲回合发送一次


class ChatOrchestrator:
    """单轮流水线：检索 → 抽取/更新问卷 → 按需规则报价。"""

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        retriever: KnowledgeRetriever | None = None,
        extractor: RequirementExtractor | None = None,
        quote_engine: QuoteEngine | None = None,
        sessions: SessionStore | None = None,
        exporter: SessionExporter | None = None,
    ) -> None:
        self.llm = llm or LLMClient()
        self.retriever = retriever or KnowledgeRetriever.build(self.llm)
        self.extractor = extractor or RequirementExtractor(self.llm)
        # Customer pricing is delivered only through approved Twilio templates.
        # Keep the optional dependency for backwards compatibility without
        # loading the staff-only pricing worksheet into the live chat process.
        self.quote_engine = quote_engine
        self.sessions = sessions or SessionStore()
        self.exporter = exporter or SessionExporter()

    def reload_knowledge(self) -> dict[str, Any]:
        """网页「教模型」保存后热重载检索索引与引导三问。"""
        from grannyflat_bot.intake.guided_flow import reload_guided_steps
        from grannyflat_bot.knowledge.retriever import KnowledgeRetriever

        self.retriever = KnowledgeRetriever.build(self.llm)
        guided = reload_guided_steps()
        return {
            "ok": True,
            "chunks": len(self.retriever.chunks),
            "guided_steps": guided.get("steps", []),
        }

    def handle(
        self,
        session_id: str,
        user_text: str,
        *,
        locale: ReplyLocale = "en",
        channel: str = "web",
        whatsapp_phone: str | None = None,
        whatsapp_profile_name: str | None = None,
    ) -> TurnResult:
        session = self.sessions.get(session_id)
        session.channel = channel
        # WhatsApp 对客一律英文（忽略 bilingual 设置，避免客户看到中文）
        locale = "en"
        session.locale = locale
        if whatsapp_phone:
            session.whatsapp_phone = whatsapp_phone.strip()
        if whatsapp_profile_name:
            session.whatsapp_profile_name = whatsapp_profile_name.strip()
        text = (user_text or "").strip()
        if not text:
            empty = (
                "请输入关于奶奶房/地块的简短说明。\n\n---\n\n"
                "Please send a short message about your granny flat plans."
                if locale == "bilingual"
                else "Please send a short message about your granny flat plans."
            )
            return TurnResult(
                reply=empty,
                requirements=session.requirements,
                questionnaire=session.requirements.as_questionnaire(),
            )

        # 客户已有任何新回复，就取消上一轮的“10 分钟无回复”计时。
        session.address_followup_due_at = None
        session.address_followup_attempts = 0
        # 客户继续提问后，尚未发出的旧机器人答案已不再适用。
        if session.pending_bot_replies:
            session.pending_bot_replies.clear()
            self.sessions.save(session, touch=False)

        # 人工接管：只记录客户消息，不调用大模型
        if session.human_mode:
            session.add_message("user", text, extra=self._customer_message_extra(text))
            session.sync_contact_fields()
            self._apply_sales_intelligence(session, {})
            self.sessions.save(session)
            return TurnResult(
                reply="",
                requirements=session.requirements,
                questionnaire=session.requirements.as_questionnaire(),
                bot_paused=True,
            )

        settings = get_settings()
        if settings.guided_intake_enabled and user_wants_skip_guided(text):
            session.guided_intake = False

        knowledge_bundle = self.retriever.context_bundle(text)
        knowledge = str(knowledge_bundle["context"])

        extracted = self.extractor.extract_turn(
            user_text=text,
            requirements=session.requirements,
            knowledge=knowledge,
            history=session.history,
            locale=locale,
        )

        # 用本轮抽取结果更新问卷，并记录本轮是否真正提供了地址信息。
        extracted_patch = extracted["requirement_patch"]
        location_supplied_now = _patch_supplies_site_location(extracted_patch)
        session.requirements = session.requirements.apply_patch(
            extracted_patch,
            source="conversation",
        )
        # 规则抽取地址/城市（中英文），写入问卷并同步通讯录
        loc_patch = extract_location_from_text(text)
        if loc_patch:
            location_supplied_now = location_supplied_now or _patch_supplies_site_location(loc_patch)
            session.requirements = session.requirements.apply_patch(
                loc_patch,
                source="location_rules",
            )
        if session.contact_type == "unknown":
            guessed = infer_contact_type(text)
            if guessed:
                session.contact_type = guessed
                role_patch: dict[str, Any] = {"contact_type": guessed}
                if guessed == "investor" and session.requirements.usage_type is None:
                    role_patch["usage_type"] = "investment"
                session.requirements = session.requirements.apply_patch(
                    role_patch, source="conversation"
                )
        if not session.requirements.postcode:
            postcode = extract_postcode_from_text(text)
            if postcode:
                location_supplied_now = True
                session.requirements = session.requirements.apply_patch(
                    {"postcode": postcode},
                    source="conversation",
                )
        if session.requirements.unit_count is None:
            unit_count = extract_unit_count_from_text(text)
            if unit_count:
                session.requirements = session.requirements.apply_patch(
                    {"unit_count": unit_count},
                    source="conversation",
                )
        if session.requirements.usage_type is None and any(
            term in text.lower()
            for term in ("invest", "investment", "investor", "投资", "投资客")
        ):
            session.requirements = session.requirements.apply_patch(
                {"usage_type": "investment"},
                source="conversation",
            )
        session.add_message("user", text, extra=self._customer_message_extra(text))

        # 新客流程：户型卡片优先；价格模板只在对应户型送达后、且不在前两轮时发送。
        quote_template: dict[str, Any] | None = None
        layout_template: dict[str, Any] | None = None
        role_template: dict[str, Any] | None = None
        try:
            layout_template = self._maybe_layout_template(
                text,
                session.requirements,
                sent_template_keys=session.sent_template_keys,
            )
            if layout_template is None:
                customer_round = sum(
                    1 for m in session.history if m.get("role") == "user"
                )
                if channel == "whatsapp":
                    role_template = self._maybe_role_template(
                        contact_type=(
                            session.contact_type
                            if session.contact_type != "unknown"
                            else session.requirements.contact_type
                        ),
                        sent_template_keys=session.sent_template_keys,
                        customer_round=customer_round,
                    )
                if role_template is None:
                    quote_template = self._maybe_price_template(
                        sent_template_keys=session.sent_template_keys,
                        customer_round=customer_round,
                    )
        except Exception:
            logger.exception("Twilio template decision failed for %s", session_id)

        bilingual = locale == "bilingual"
        quote: QuoteResult | None = None
        reply = extracted["reply"]

        # Preserve the answer regardless of punctuation or intake progress.
        # Only use a guided question when the model returned no usable answer.
        guided_on = settings.guided_intake_enabled and session.guided_intake
        step = next_missing_step(session.requirements) if guided_on else None
        if not (reply or "").strip():
            previous_replies = {m["content"] for m in session.history if m["role"] == "assistant"}
            candidate = format_guided_question(step, locale=locale) if step else ""
            reply = candidate if candidate and candidate not in previous_replies else format_intake_complete(locale=locale)

        if guided_on and step is None:
            session.guided_quote_sent = True

        # Follow-up must not depend on intake completion. Do not append questions:
        # the model has the requirements/history and already owns the next question.
        if extracted.get("escalate_to_human"):
            session.escalated = True
            session.escalation_reason = extracted.get("escalation_reason") or "site review"

        from grannyflat_bot.llm.translate import english_only_for_customer

        reply = english_only_for_customer(reply, llm=self.llm)
        reply = _compact_customer_reply(
            reply,
            history=session.history,
            suppress_questions=role_template is not None,
        )

        # 询问州、地址、城区或邮编后启动 10 分钟计时。客户一回复即在下一轮开头取消。
        if channel == "whatsapp" and _asks_for_site_location(reply):
            session.address_followup_due_at = (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat()
            session.address_followup_attempts = 0

        assistant_extra = self._assistant_message_extra(reply)
        assistant_extra["knowledge_sources"] = knowledge_bundle["sources"]
        assistant_extra["knowledge_confidence"] = knowledge_bundle["confidence"]
        session.add_message("assistant", reply, extra=assistant_extra)
        session.sync_contact_fields()
        self._apply_sales_intelligence(session, extracted)
        self.sessions.save(session)

        exports: dict[str, Any] | None = None
        if get_recording_state().enabled:
            paths = self.exporter.export_session(
                session, channel=channel, locale=locale
            )
            self.sessions.save(session)
            exports = paths.to_dict()

        return TurnResult(
            reply=reply,
            requirements=session.requirements,
            quote=quote,
            quote_template=quote_template,
            layout_template=layout_template,
            role_template=role_template,
            escalate_to_human=bool(extracted.get("escalate_to_human")),
            escalation_reason=extracted.get("escalation_reason"),
            questionnaire=session.requirements.as_questionnaire(),
            exports=exports,
        )

    def get_session_view(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
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
        }

    def refresh_contact_ai_summary(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        summary_text, summary_source = generate_ai_summary(
            self.llm,
            requirements=session.requirements,
            history=session.history,
            sales_stage=session.sales_stage,
        )
        session.ai_summary = summary_text
        session.ai_summary_updated_at = datetime.now(timezone.utc).isoformat()
        session.ai_summary_source = summary_source
        session.sync_contact_fields()
        self.sessions.save(session, touch=False)
        return self.get_contact(session_id)

    def _apply_sales_intelligence(
        self, session: Session, extracted: dict[str, Any]
    ) -> None:
        model_summary = safe_internal_text(
            extracted.get("crm_summary"), max_length=1000
        )
        if model_summary:
            session.ai_summary = model_summary
            session.ai_summary_source = "llm"
            session.ai_summary_updated_at = datetime.now(timezone.utc).isoformat()
        else:
            session.ai_summary = deterministic_summary(
                session.requirements,
                history=session.history,
                sales_stage=session.sales_stage,
            )
            session.ai_summary_source = "rules"
            session.ai_summary_updated_at = datetime.now(timezone.utc).isoformat()
        if not session.next_action_manual:
            session.next_action = (
                safe_internal_text(extracted.get("next_action"), max_length=240)
                or default_next_action(session.sales_stage)
            )
        if not session.follow_up_due_manual:
            session.follow_up_due_at = follow_up_datetime(
                extracted.get("follow_up_in_hours"),
                sales_stage=session.sales_stage,
            )

    def export_session_files(
        self,
        session_id: str,
        *,
        channel: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if channel:
            session.channel = channel
        if locale:
            session.locale = locale
        paths = self.exporter.export_session(
            session,
            channel=session.channel,
            locale=session.locale,
        )
        self.sessions.save(session)
        return paths.to_dict()

    def list_contacts(
        self,
        *,
        customer_status: str | None = None,
        sales_stage: str | None = None,
        archived: bool | None = False,
    ) -> list[dict[str, Any]]:
        """通讯录列表（国内工作台用）。

        archived=False：仅活跃；True：仅归档；None：全部。
        头像编号按客户首次出现时间（created_at）先后：1、2、3…
        """
        sessions = [
            s for s in self.sessions.list_all()
            if not s.session_id.startswith("teach-")
        ]
        by_created = sorted(sessions, key=lambda s: s.created_at or "")
        avatar_nos = {s.session_id: i + 1 for i, s in enumerate(by_created)}

        rows = []
        for session in sessions:
            if archived is False and session.archived:
                continue
            if archived is True and not session.archived:
                continue
            if session.sync_contact_fields():
                self.sessions.save(session, touch=False)
            rows.append(
                self._contact_summary(session, avatar_no=avatar_nos.get(session.session_id))
            )
        # 排序规则：有真实对话的在前（按 sort_key = max(最后消息时间, 资料编辑时间) 降序）；
        # 没有任何对话的批量导入联系人沉到列表底部，避免顶部堆一批空白条目。
        # 显示仍用 updated_at（真实最后消息时间），二者不可混用。
        rows.sort(
            key=lambda r: (
                1 if r.get("has_messages") else 0,
                r.get("sort_key") or r.get("updated_at") or "",
            ),
            reverse=True,
        )
        if customer_status and customer_status not in ("", "all"):
            rows = [r for r in rows if r.get("customer_status") == customer_status]
        if sales_stage and sales_stage not in ("", "all"):
            rows = [r for r in rows if r.get("sales_stage") == sales_stage]
        return rows

    def pipeline_summary(self, *, archived: bool = False) -> dict[str, Any]:
        rows = self.list_contacts(archived=archived)
        now = datetime.now(timezone.utc)
        overdue = 0
        due_soon = 0
        for row in rows:
            if row.get("sales_stage") == "closed":
                continue
            due = _parse_optional_datetime(row.get("follow_up_due_at"))
            if not due:
                continue
            if due < now:
                overdue += 1
            elif due <= now + timedelta(days=1):
                due_soon += 1
        return {
            "total": len(rows),
            "overdue": overdue,
            "due_within_24h": due_soon,
            "average_qualification_score": round(
                sum(int(row.get("qualification_score") or 0) for row in rows)
                / max(len(rows), 1),
                1,
            ),
            "lead_temperatures": [
                {
                    "code": code,
                    "label": label,
                    "count": sum(
                        1 for row in rows if row.get("lead_temperature") == code
                    ),
                }
                for code, label in LEAD_TEMPERATURE_LABELS.items()
            ],
            "stages": [
                {
                    "code": code,
                    "label": label,
                    "count": sum(1 for row in rows if row.get("sales_stage") == code),
                }
                for code, label in SALES_STAGES.items()
            ],
        }

    def list_sales_tasks(self) -> dict[str, Any]:
        sessions = [
            session
            for session in self.sessions.list_all()
            if not session.session_id.startswith("teach-")
        ]
        for session in sessions:
            if session.sync_contact_fields():
                self.sessions.save(session, touch=False)
        tasks = build_sales_tasks(sessions)
        avatar_nos = {
            session.session_id: index + 1
            for index, session in enumerate(
                sorted(sessions, key=lambda item: item.created_at or "")
            )
        }
        contacts_by_id = {
            session.session_id: self._contact_summary(
                session, avatar_no=avatar_nos.get(session.session_id)
            )
            for session in sessions
        }
        enriched = [
            {**task, "contact": contacts_by_id.get(task["session_id"], {})}
            for task in tasks
        ]
        return {"tasks": enriched, "summary": task_summary(tasks)}

    def get_contact(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        # 只读刷新：无字段变化则不 save，避免每 5s 轮询把会话顶到列表顶部
        if session.sync_contact_fields():
            self.sessions.save(session)
        avatar_no = self._avatar_no_for(session_id)
        return {
            **self._contact_summary(session, avatar_no=avatar_no),
            "history": history_with_receipts(session),
            "questionnaire": session.requirements.as_questionnaire(),
            "requirements": session.requirements.model_dump(mode="json"),
            "last_quote": session.last_quote,
            "exports": session.exports,
            "escalation_reason": session.escalation_reason,
            "sales_stage_history": session.sales_stage_history,
            "ai_summary": session.ai_summary,
            "ai_summary_updated_at": session.ai_summary_updated_at,
            "ai_summary_source": session.ai_summary_source,
            "qualification_missing": session.qualification_missing,
        }

    def mark_contact_read(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session.mark_read():
            # 已读状态不改变最后消息时间，也不改变通讯录排序。
            self.sessions.save(session, touch=False)
        return {"ok": True, "session_id": session_id, "unread_count": session.unread_count()}

    def set_archived(self, session_id: str, archived: bool) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        session.archived = bool(archived)
        self.sessions.save(session)
        return self.get_contact(session_id)

    def delete_contact(self, session_id: str) -> dict[str, Any]:
        ok = self.sessions.delete(session_id)
        return {"ok": ok, "session_id": session_id}

    def _avatar_no_for(self, session_id: str) -> int | None:
        sessions = [
            s for s in self.sessions.list_all()
            if not s.session_id.startswith("teach-")
        ]
        by_created = sorted(sessions, key=lambda s: s.created_at or "")
        for i, s in enumerate(by_created):
            if s.session_id == session_id:
                return i + 1
        return None

    def update_contact(
        self,
        session_id: str,
        *,
        name: str | None = None,
        personal_name: str | None = None,
        surname: str | None = None,
        given: str | None = None,
        state: str | None = None,
        city: str | None = None,
        postcode: str | None = None,
        address: str | None = None,
        contact_type: str | None = None,
        tags: list[str] | None = None,
        workflow_status: str | None = None,
        sales_stage: str | None = None,
        sales_stage_auto: bool | None = None,
        lead_temperature: str | None = None,
        lead_temperature_auto: bool | None = None,
        next_action: str | None = None,
        next_action_auto: bool | None = None,
        follow_up_due_at: str | None = None,
        follow_up_due_auto: bool | None = None,
        customer_status: str | None = None,
        customer_status_auto: bool | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        patch: dict[str, Any] = {}
        # 个人姓名：写入问卷，不锁死显示名，仍按「城市/州+姓名」规则生成
        if personal_name is not None:
            cleaned_personal = personal_name.strip()
            patch["contact_name"] = cleaned_personal or None
            session.name_manual = False
        if surname is not None or given is not None:
            if surname is not None:
                session.contact_surname = surname.strip() or None
            if given is not None:
                session.contact_given = given.strip() or None
            combined = " ".join(
                item for item in (session.contact_surname, session.contact_given) if item
            )
            patch["contact_name"] = combined or None
            session.name_manual = False
        if name is not None:
            cleaned = name.strip()
            session.display_name = cleaned or session.display_name
            session.name_manual = bool(cleaned)
            if cleaned:
                patch["contact_name"] = cleaned
        if state is not None:
            session.state = state.strip().upper() or None
            if session.state:
                patch["state"] = session.state
        if city is not None:
            session.city = city.strip() or None
            if session.city:
                patch["suburb_or_lga"] = session.city
        if postcode is not None:
            session.postcode = postcode.strip() or None
            if session.postcode:
                patch["postcode"] = session.postcode
        if address is not None:
            session.address = normalize_address(address, session.postcode) or None
            # Keep the manual field authoritative, including an explicit clear.
            patch["street_address"] = session.address
        if contact_type is not None and contact_type in CONTACT_TYPES:
            session.contact_type = contact_type
            patch["contact_type"] = contact_type
        if tags is not None:
            session.tags = [t.strip() for t in tags if t and t.strip()]
        chosen_workflow = workflow_status or category
        if chosen_workflow and chosen_workflow in WORKFLOW_STATUSES:
            session.workflow_status = chosen_workflow
        if sales_stage_auto:
            session.sales_stage_manual = False
        elif sales_stage is not None:
            cleaned_stage = sales_stage.strip()
            if cleaned_stage not in SALES_STAGES:
                raise ValueError("invalid sales_stage")
            session.set_sales_stage(
                cleaned_stage,
                source="agent",
                note="客服手动更新",
                manual=True,
            )
        if lead_temperature_auto:
            session.lead_temperature_manual = False
        elif lead_temperature is not None:
            cleaned_temperature = lead_temperature.strip()
            if cleaned_temperature not in LEAD_TEMPERATURE_LABELS:
                raise ValueError("invalid lead_temperature")
            session.lead_temperature = cleaned_temperature
            session.lead_temperature_manual = True
        if next_action_auto:
            session.next_action_manual = False
            session.next_action = None
        elif next_action is not None:
            cleaned_action = next_action.strip()
            session.next_action = cleaned_action or None
            session.next_action_manual = bool(cleaned_action)
        if follow_up_due_auto:
            session.follow_up_due_manual = False
        elif follow_up_due_at is not None:
            cleaned_due = follow_up_due_at.strip()
            if cleaned_due:
                session.follow_up_due_at = _parse_optional_datetime(
                    cleaned_due, strict=True
                ).isoformat()
                session.follow_up_due_manual = True
            else:
                session.follow_up_due_at = None
                session.follow_up_due_manual = False
        if customer_status_auto:
            session.customer_status_manual = False
        elif customer_status is not None:
            cleaned_status = customer_status.strip()
            if cleaned_status in ("", "auto"):
                session.customer_status_manual = False
            elif cleaned_status in CUSTOMER_STATUSES:
                session.customer_status = cleaned_status
                session.customer_status_manual = True
        if patch:
            session.requirements = session.requirements.apply_patch(
                patch,
                source="agent",
            )
        session.sync_contact_fields()
        # 人工编辑过资料 -> 推进排序键，让该客户排到列表前面。
        # 这里只动 contact_edited_at，不动 updated_at，
        # 否则列表会把「编辑时间」当成「最后消息时间」显示，旧会话显示成今天。
        session.mark_contact_edited()
        self.sessions.save(session)
        return self.get_contact(session_id)

    def set_human_mode(self, session_id: str, enabled: bool) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        session.human_mode = enabled
        if enabled:
            session.workflow_status = "human"
            session.pending_bot_replies.clear()
        else:
            session.sync_contact_fields()
        self.sessions.save(session)
        return self.get_contact(session_id)

    PRICE_TEMPLATE_SIDS = {
        "60": "HXf42f7b939af24efa5cc104a1cac356ba",  # 60price
        "70": "HX917e8386f1811c8ecd83e6175b386b65",  # 70price
    }

    LAYOUT_TEMPLATE_SIDS = {
        "15": "HX2e753f9a0f3b0012660d6b20eb7f4c57",  # 15s
        "20": "HX083c36184bfaedacf5b09154de3fc972",  # 20s
        "30": "HX5935b075cb6488773f7f2e889c3bfe2f",  # 30s
        "60": "HX9b19bdc5b8723d7b9df3e4cb9e3cbb03",  # 60s
        "70": "HX1450d67c4d601072fe5248c5eb0f87ae",  # 70s
        "80": "HX5a7c1333fa67dd2c37cf292863077a00",  # 80s
    }

    INFO_TEMPLATE_SIDS = {
        "role": "HXad1b8c385087a29613227481a2953d15",
        "introduce": "HX3d2798efe159204f50ba1f747dbfaf6d",
        "shiping": "HXcddfac471c4531d8746a3474538471bc",
        "address": "HX2c90e2ae47b7f28b6a1e4df94d7fa98f",
    }

    # 客服在聊天框输入 "/60s" 等命令时，发送对应的真实 WhatsApp 模板（带图片卡片）。
    AGENT_TEMPLATE_COMMAND_RE = re.compile(r"^/\s*([A-Za-z0-9]+)\s*$")

    def _agent_template_map(self) -> dict[str, str]:
        """客服斜杠命令 → 模板 SID（含户型、价格、身份选择和介绍模板）。"""
        mapping: dict[str, str] = {}
        for size, sid in self.LAYOUT_TEMPLATE_SIDS.items():
            mapping[f"{size}s"] = sid
        for size, sid in self.PRICE_TEMPLATE_SIDS.items():
            mapping[f"{size}price"] = sid
        mapping.update(self.INFO_TEMPLATE_SIDS)
        return mapping

    def _maybe_role_template(
        self,
        *,
        contact_type: str | None,
        sent_template_keys: list[str],
        customer_round: int,
    ) -> dict[str, Any] | None:
        """首次有效答复后，在没有其他模板占用的回合发送一次身份选择器。"""
        if customer_round < 2 or "role" in sent_template_keys:
            return None
        if contact_type and contact_type != "unknown":
            return None
        return {
            "sid": self.INFO_TEMPLATE_SIDS["role"],
            "key": "role",
            "name": "role",
            "variables": None,
        }

    def _match_agent_template_command(self, text: str) -> tuple[str, str] | None:
        """整条消息形如 "/60s" 且名称命中模板库时返回 (key, sid)，否则 None。"""
        m = self.AGENT_TEMPLATE_COMMAND_RE.match((text or '').strip())
        if not m:
            return None
        key = m.group(1).lower()
        sid = self._agent_template_map().get(key)
        if not sid:
            return None
        return key, sid

    def _maybe_price_template(
        self,
        *,
        sent_template_keys: list[str],
        customer_round: int,
    ) -> dict[str, Any] | None:
        """户型卡片送达后才发对应价格模板；前两轮对话不发价格。

        价格与最近发送的户型对应：60s→60price、70s→70price；
        80s/Box 系列没有对应价格模板，不发。
        """
        if customer_round <= 2:
            return None
        layout_keys = [
            key
            for key in sent_template_keys
            if key.endswith("s") and key[:-1] in self.LAYOUT_TEMPLATE_SIDS
        ]
        if not layout_keys:
            return None
        size = layout_keys[-1][:-1]
        key = f"{size}price"
        if key in sent_template_keys:
            return None
        sid = self.PRICE_TEMPLATE_SIDS.get(size)
        if not sid:
            return None
        return {
            "sid": sid,
            "size": size,
            "key": key,
            "name": key,
            "variables": None,
        }

    def _maybe_layout_template(
        self,
        user_text: str,
        req: RequirementState,
        *,
        sent_template_keys: list[str],
    ) -> dict[str, Any] | None:
        """一次只选一个户型模板；客户明确面积优先，否则按州选择 60s/70s。"""
        explicit_size = _explicit_layout_size(user_text)
        if explicit_size is None:
            explicit_size = _layout_size_from_area(req.approx_floor_area_m2)
        if explicit_size:
            size = explicit_size
        else:
            # 若此前已经自动发过户型，只有客户明确改问另一面积时才再发。
            if any(key.endswith("s") and key[:-1] in self.LAYOUT_TEMPLATE_SIDS for key in sent_template_keys):
                return None
            state = _resolved_state(req)
            if not state:
                return None
            size = "70" if state in {"WA", "QLD"} else "60"

        key = f"{size}s"
        if key in sent_template_keys:
            return None
        sid = self.LAYOUT_TEMPLATE_SIDS.get(size)
        if not sid:
            return None
        return {
            "sid": sid,
            "size": size,
            "key": key,
            "name": key,
            "variables": None,
        }

    def mark_template_sent(
        self,
        session_id: str,
        template: dict[str, Any],
        *,
        message_sid: str | None = None,
    ) -> None:
        """模板被 Twilio 接受后持久化，防止同一会话重复自动发送。"""
        session = self.sessions.get(session_id)
        key = str(template.get("key") or template.get("name") or "").strip()
        if key and key not in session.sent_template_keys:
            session.sent_template_keys.append(key)
        if key == "60s":
            session.address_followup_due_at = None
            session.address_followup_attempts = 0
        session.add_message(
            "assistant",
            f"[WhatsApp template sent: {key or template.get('sid')}]",
            extra={
                "template_name": key or None,
                "template_sid": template.get("sid"),
                "twilio_message_sid": message_sid,
            },
        )
        self.sessions.save(session)

    def _customer_message_extra(self, text: str) -> dict[str, Any]:
        """客户英文消息附带中文译文，供客服工作台阅读。"""
        from grannyflat_bot.llm.translate import en_to_zh, looks_primarily_english

        extra: dict[str, Any] = {}
        if looks_primarily_english(text):
            zh = en_to_zh(text, llm=self.llm)
            if zh and zh.strip() != text.strip():
                extra["content_zh"] = zh
                extra["content_en"] = text
                extra["translated"] = True
        return extra

    def _assistant_message_extra(self, text: str) -> dict[str, Any]:
        """纯英文机器人回复附中文译文（双语 --- 格式跳过）。"""
        from grannyflat_bot.llm.translate import contains_chinese, en_to_zh, looks_primarily_english

        extra: dict[str, Any] = {}
        if "\n---\n" in text or contains_chinese(text):
            return extra
        if looks_primarily_english(text):
            zh = en_to_zh(text, llm=self.llm)
            if zh and zh.strip() != text.strip():
                extra["content_zh"] = zh
                extra["translated"] = True
        return extra

    def agent_send_message(
        self,
        session_id: str,
        text: str,
        *,
        agent_name: str = "客服",
    ) -> dict[str, Any]:
        """人工发送消息；中文自动译成英文对客；WhatsApp 经 Twilio 外发。

        特殊指令：聊天框输入「发送60」→ 以商品目录形式发送 A60/B60/C60/D60。
        """
        from grannyflat_bot.catalog.packs import match_catalog_command
        from grannyflat_bot.llm.translate import (
            contains_chinese,
            english_only_for_customer,
            zh_to_en,
        )

        session = self.sessions.get(session_id)
        original = (text or "").strip()
        if not original:
            raise ValueError("message cannot be empty")

        pack = match_catalog_command(original)
        if pack is not None:
            return self._agent_send_catalog(session, pack, agent_name=agent_name)

        template_cmd = self._match_agent_template_command(original)
        if template_cmd is not None:
            key, sid = template_cmd
            return self._agent_send_template(session, key, sid, agent_name=agent_name)

        outbound = original
        extra: dict[str, Any] = {"agent_name": agent_name}
        if contains_chinese(original):
            outbound = zh_to_en(original, llm=self.llm)
            extra["content_zh"] = original
            extra["translated"] = True
        # WhatsApp / 对客渠道：确保发出去的没有中文
        outbound = english_only_for_customer(outbound, llm=self.llm)
        if extra.get("translated") or contains_chinese(original):
            extra["content_en"] = outbound

        session.add_message("agent", outbound, extra=extra)
        session.human_mode = True
        session.workflow_status = "human"
        self.sessions.save(session)
        try:
            learned = self._maybe_learn_from_agent(session, outbound, agent_name=agent_name)
        except Exception:
            learned = {"saved": False, "error": "learn_failed"}
        delivery = self._deliver_agent_message(session, outbound)
        return {**self.get_contact(session_id), "delivery": delivery, "learned": learned}

    def _agent_send_template(
        self,
        session: Session,
        key: str,
        sid: str,
        *,
        agent_name: str,
    ) -> dict[str, Any]:
        """客服斜杠命令：向客户发送真实 WhatsApp 模板（户型为带图片卡片）。"""
        session.human_mode = True
        session.workflow_status = "human"
        if key not in session.sent_template_keys:
            session.sent_template_keys.append(key)
        if key == "60s":
            session.address_followup_due_at = None
            session.address_followup_attempts = 0
        session.add_message(
            "agent",
            f"[WhatsApp template sent: {key}]",
            extra={
                "agent_name": agent_name,
                "template_name": key,
                "template_sid": sid,
            },
        )
        self.sessions.save(session)
        delivery = self._deliver_agent_template(session, key, sid)
        return {
            **self.get_contact(session.session_id),
            "delivery": delivery,
            "template": {"key": key, "sid": sid},
        }

    def _deliver_agent_template(
        self, session: Session, key: str, content_sid: str
    ) -> dict[str, Any]:
        """向客户渠道投递模板消息（目前支持 Twilio WhatsApp）。"""
        base: dict[str, Any] = {"type": "template", "template_name": key}
        if not session.session_id.startswith("wa-"):
            return {
                **base,
                "channel": session.channel,
                "sent": False,
                "note": "web/cli — template recorded only",
            }
        phone = session.session_id.removeprefix("wa-")
        try:
            from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
            from grannyflat_bot.config import get_settings

            settings = get_settings()
            if not settings.twilio_configured:
                return {
                    **base,
                    "channel": "whatsapp",
                    "sent": False,
                    "note": "Twilio not configured; template recorded only",
                }
            if not settings.twilio_whatsapp_enabled:
                return {
                    **base,
                    "channel": "whatsapp",
                    "sent": False,
                    "note": "WhatsApp paused (TWILIO_WHATSAPP_ENABLED=0); template recorded only",
                }
            channel = TwilioWhatsAppChannel()
            import asyncio

            message_at = session.history[-1]["at"] if session.history else None
            async def _send() -> None:
                await send_tracked(channel, session, content_sid=content_sid, message_at=message_at)

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(_send())
                else:
                    loop.run_until_complete(_send())
            except RuntimeError:
                asyncio.run(_send())
            return {**base, "channel": "whatsapp", "sent": True, "to": phone}
        except Exception as exc:
            return {**base, "channel": "whatsapp", "sent": False, "error": str(exc)[:200]}

    def _agent_send_catalog(
        self,
        session: Session,
        pack: Any,
        *,
        agent_name: str,
    ) -> dict[str, Any]:
        names = ", ".join(item.name for item in pack.items)
        session.human_mode = True
        session.workflow_status = "human"

        delivery = self._deliver_catalog(session, pack)
        if delivery.get("sent"):
            mode = (delivery.get("mode") or "") if isinstance(delivery, dict) else ""
            # mode may be nested in raw - check delivery keys from _deliver_catalog
            raw_mode = ""
            if isinstance(delivery, dict):
                raw_mode = str(delivery.get("twilio_mode") or delivery.get("mode") or "")
            if raw_mode == "catalog_link_fallback" or delivery.get("fallback") == "catalog_link":
                note = (
                    f"商品卡片被 Twilio 拒绝(63013)。已改发目录链接给客户"
                    f"（{names}）。客户可点击链接浏览。"
                )
            else:
                note = f"已向客户 WhatsApp 发送商品目录「{pack.title}」：{names}"
        else:
            err = delivery.get("error") or delivery.get("note") or "未知错误"
            note = (
                f"商品目录未发出（{names}）。原因：{err}"
            )

        session.add_message(
            "agent",
            note,
            extra={
                "agent_name": agent_name,
                "catalog_key": pack.key,
                "catalog_items": [
                    {"name": i.name, "content_id": i.content_id} for i in pack.items
                ],
                "delivery": delivery,
            },
        )
        self.sessions.save(session)
        return {
            **self.get_contact(session.session_id),
            "delivery": delivery,
            "learned": {"saved": False, "skipped": "catalog_command"},
        }

    def _deliver_catalog(self, session: Session, pack: Any) -> dict[str, Any]:
        if not session.session_id.startswith("wa-"):
            return {
                "channel": session.channel,
                "sent": False,
                "type": "catalog",
                "note": (
                    "当前选中的是网页演示会话，无法推送 WhatsApp 商品目录。"
                    "请在左侧选择带手机号的 WhatsApp 客户（头像旁/副标题有 WhatsApp 号码），"
                    "再输入「发送60」。"
                ),
            }
        phone = session.session_id.removeprefix("wa-")
        try:
            from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
            from grannyflat_bot.config import get_settings

            settings = get_settings()
            if not settings.twilio_configured:
                return {
                    "channel": "whatsapp",
                    "sent": False,
                    "type": "catalog",
                    "note": "Twilio 未配置，无法外发 WhatsApp",
                }
            if not settings.twilio_whatsapp_enabled:
                return {
                    "channel": "whatsapp",
                    "sent": False,
                    "type": "catalog",
                    "note": "WhatsApp 已暂停（TWILIO_WHATSAPP_ENABLED=0）",
                }
            channel = TwilioWhatsAppChannel(settings)
            import asyncio

            async def _send() -> dict[str, Any]:
                return await channel.send_catalog(to=phone, pack=pack)

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(lambda: asyncio.run(_send())).result(
                            timeout=60
                        )
                else:
                    result = loop.run_until_complete(_send())
            except RuntimeError:
                result = asyncio.run(_send())
            return {
                "channel": "whatsapp",
                "sent": True,
                "type": "catalog",
                "to": phone,
                "pack": pack.key,
                "items": [i.name for i in pack.items],
                "content_ids": [i.content_id for i in pack.items],
                "sid": (result or {}).get("sid"),
                "mode": (result or {}).get("mode"),
                "twilio_mode": (result or {}).get("mode"),
                "fallback": (
                    "catalog_link"
                    if (result or {}).get("mode") == "catalog_link_fallback"
                    else None
                ),
                "catalog_url": (result or {}).get("catalog_url"),
                "note": (result or {}).get("note"),
            }
        except Exception as exc:
            return {
                "channel": "whatsapp",
                "sent": False,
                "type": "catalog",
                "error": str(exc)[:400],
            }

    def _maybe_learn_from_agent(
        self,
        session: Session,
        agent_reply: str,
        *,
        agent_name: str,
    ) -> dict[str, Any]:
        from grannyflat_bot.agent_learn import record_agent_exchange

        result = record_agent_exchange(
            session_id=session.session_id,
            channel=session.channel,
            history=session.history,
            agent_reply=agent_reply,
            agent_name=agent_name,
            llm=self.llm,
        )
        if result.get("saved"):
            self.reload_knowledge()
        return result

    def _deliver_agent_message(self, session: Session, text: str) -> dict[str, Any]:
        """向客户渠道投递人工消息（目前支持 Twilio WhatsApp）。"""
        if not session.session_id.startswith("wa-"):
            return {"channel": session.channel, "sent": False, "note": "web/cli — message stored only"}
        phone = session.session_id.removeprefix("wa-")
        try:
            from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
            from grannyflat_bot.config import get_settings

            settings = get_settings()
            if not settings.twilio_configured:
                return {
                    "channel": "whatsapp",
                    "sent": False,
                    "note": "Twilio not configured; message stored for agent console only",
                }
            if not settings.twilio_whatsapp_enabled:
                return {
                    "channel": "whatsapp",
                    "sent": False,
                    "note": "WhatsApp paused (TWILIO_WHATSAPP_ENABLED=0); message stored only",
                }
            channel = TwilioWhatsAppChannel()
            import asyncio

            message_at = session.history[-1]["at"] if session.history else None
            async def _send():
                return await send_tracked(channel, session, text=text, message_at=message_at)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(_send())
                else:
                    loop.run_until_complete(_send())
            except RuntimeError:
                asyncio.run(_send())
            return {"channel": "whatsapp", "sent": True, "to": phone}
        except Exception as exc:
            return {"channel": "whatsapp", "sent": False, "error": str(exc)[:200]}

    def _contact_summary(
        self, session: Session, *, avatar_no: int | None = None
    ) -> dict[str, Any]:
        phone = session.whatsapp_phone or extract_whatsapp_phone(session.session_id)
        manual_name = session.display_name if session.name_manual else None
        name = infer_display_name(
            session.requirements,
            session_id=session.session_id,
            manual_name=manual_name,
            whatsapp_profile_name=session.whatsapp_profile_name,
            name_manual=session.name_manual,
            city=session.city,
            state=session.state,
            postcode=session.postcode,
            address=session.address,
            whatsapp_phone=phone,
        )
        personal = resolve_personal_name(
            session.requirements,
            whatsapp_profile_name=session.whatsapp_profile_name,
        )
        location = format_location_summary(
            state=session.state,
            city=session.city,
            postcode=session.postcode,
            address=session.address,
        )
        return {
            "session_id": session.session_id,
            "name": name,
            "personal_name": personal,
            "surname": session.contact_surname or _split_personal_name(personal)[0],
            "given": session.contact_given or _split_personal_name(personal)[1],
            "name_manual": session.name_manual,
            "avatar_no": avatar_no,
            "whatsapp_phone": phone,
            "whatsapp_profile_name": session.whatsapp_profile_name,
            "state": session.state,
            "city": session.city,
            "postcode": session.postcode,
            "address": normalize_address(session.address, session.postcode),
            "location_summary": location,
            "contact_type": session.contact_type,
            "contact_type_label": contact_type_label(session.contact_type),
            "workflow_status": session.workflow_status,
            "workflow_label": workflow_status_label(session.workflow_status),
            "sales_stage": session.sales_stage,
            "sales_stage_label": sales_stage_label(session.sales_stage),
            "sales_stage_manual": session.sales_stage_manual,
            "next_action": session.next_action,
            "next_action_manual": session.next_action_manual,
            "follow_up_due_at": session.follow_up_due_at,
            "follow_up_due_manual": session.follow_up_due_manual,
            "qualification_score": session.qualification_score,
            "qualification_level": session.qualification_level,
            "qualification_label": QUALIFICATION_LABELS.get(
                session.qualification_level, session.qualification_level
            ),
            "lead_temperature": session.lead_temperature,
            "lead_temperature_manual": session.lead_temperature_manual,
            "lead_temperature_label": LEAD_TEMPERATURE_LABELS.get(
                session.lead_temperature, session.lead_temperature
            ),
            "lead_priority_reasons": session.lead_priority_reasons,
            "customer_status": session.customer_status,
            "customer_status_label": customer_status_label(session.customer_status),
            "customer_status_manual": session.customer_status_manual,
            "tags": session.tags,
            "category": session.contact_type,
            "category_label": contact_type_label(session.contact_type),
            "channel": session.channel,
            "human_mode": session.human_mode,
            "escalated": session.escalated,
            "archived": session.archived,
            "message_count": session.real_message_count(),
            "unread_count": session.unread_count(),
            "has_messages": session.has_real_message(),
            "last_message": session.last_message_preview(),
            "search_text": " ".join(
                str(item.get("content") or "")
                for item in session.history
                if item.get("role") in {"user", "assistant"}
            ),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "sort_key": session.sort_key(),
            "contact_edited_at": session.contact_edited_at,
        }


def _patch_supplies_site_location(patch: Any) -> bool:
    """州名本身用于选户型；价格触发需城区、邮编或街道地址。"""
    if not isinstance(patch, dict):
        return False
    location_fields = {
        "suburb_or_lga",
        "postcode",
        "street_address",
        "address",
        "city",
        "suburb",
    }
    return any(patch.get(key) for key in location_fields)


def _split_personal_name(value: str | None) -> tuple[str, str]:
    """Keep imported surname and given-name values separate for the UI."""
    text = str(value or "").strip()
    if not text or text.isdigit():
        return "", ""
    parts = text.split()
    if len(parts) > 1:
        return parts[0], " ".join(parts[1:])
    if any("\u4e00" <= ch <= "\u9fff" for ch in text) and len(text) > 1:
        return text[:1], text[1:]
    return "", text


def _parse_optional_datetime(value: Any, *, strict: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if strict:
            raise ValueError("follow_up_due_at must be an ISO-8601 datetime")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _explicit_layout_size(text: str) -> str | None:
    """从本轮文字识别客户明确点名的单一面积/型号。"""
    value = (text or "").lower().replace("‑", "-").replace("–", "-")
    patterns = (
        r"\bbox\s*-?\s*(15|20|30)\b",
        r"\b[a-d]\s*-?\s*(60|70|80)\b",
        r"\b(15|20|30|60|70|80)\s*(?:m\s*(?:2|²)|sqm|sq\.?\s*m|square\s+met(?:er|re)s?)\b",
        r"\b(15|20|30|60|70|80)\s*(?:m2\s*)?(?:floor\s*)?plans?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _layout_size_from_area(area: float | None) -> str | None:
    if area is None:
        return None
    for size in (15, 20, 30, 60, 70, 80):
        if abs(float(area) - size) <= 2:
            return str(size)
    return None


def _postcode_state(postcode: str | None) -> str | None:
    match = re.search(r"\b(\d{4})\b", postcode or "")
    if not match:
        return None
    number = int(match.group(1))
    if 200 <= number <= 299 or 2600 <= number <= 2618 or 2900 <= number <= 2920:
        return "ACT"
    if 800 <= number <= 999:
        return "NT"
    if 1000 <= number <= 2599 or 2619 <= number <= 2899 or 2921 <= number <= 2999:
        return "NSW"
    if 3000 <= number <= 3999 or 8000 <= number <= 8999:
        return "VIC"
    if 4000 <= number <= 4999 or 9000 <= number <= 9999:
        return "QLD"
    if 5000 <= number <= 5999:
        return "SA"
    if 6000 <= number <= 6999:
        return "WA"
    if 7000 <= number <= 7999:
        return "TAS"
    return None


def _resolved_state(req: RequirementState) -> str | None:
    state = getattr(req.state, "value", req.state)
    if state:
        normalized = str(state).strip().upper()
        if normalized in {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}:
            return normalized
    return _postcode_state(req.postcode)


_REPLY_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")


def _reply_sentences(reply: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", (reply or "").strip())
    return [part.strip() for part in _REPLY_SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]


def _sentence_key(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", sentence.lower())


def _compact_customer_reply(
    reply: str,
    *,
    history: list[dict[str, Any]],
    suppress_questions: bool = False,
) -> str:
    """去掉近期逐字重复句，并把 WhatsApp 回复收敛到最多三句。"""
    sentences = _reply_sentences(reply)
    if not sentences:
        return "Thanks — how can I help with your granny flat project?"

    previous_keys = {
        _sentence_key(sentence)
        for message in history[-12:]
        if message.get("role") in {"assistant", "agent"}
        for sentence in _reply_sentences(str(message.get("content") or ""))
        if _sentence_key(sentence)
    }
    unique: list[str] = []
    current_keys: set[str] = set()
    for sentence in sentences:
        key = _sentence_key(sentence)
        if not key or key in current_keys or key in previous_keys:
            continue
        current_keys.add(key)
        unique.append(sentence)

    # 若客户重复原问题，完整答案可能与上一轮相同；不能因此发空消息。
    if not unique:
        unique = sentences
    if suppress_questions:
        unique = [sentence for sentence in unique if not sentence.rstrip().endswith("?")]
        if not unique:
            return "Thanks — that helps."

    if len(unique) > 3:
        question = next(
            (sentence for sentence in reversed(unique) if sentence.rstrip().endswith("?")),
            None,
        )
        unique = unique[:2] + ([question] if question and question not in unique[:2] else [])
    return " ".join(unique[:3]).strip()


def _asks_for_site_location(reply: str) -> bool:
    """识别机器人是否在等待客户给州、地址、城区或邮编。"""
    lower = (reply or "").lower()
    if "?" not in lower:
        return False
    phrases = (
        "which state",
        "what state",
        "site address",
        "street address",
        "suburb",
        "postcode",
        "post code",
        "where is the site",
        "where is your project",
    )
    return any(phrase in lower for phrase in phrases)


def _user_wants_quote(text: str) -> bool:
    """粗检客户是否在要价格（英文关键词为主，兼顾常见中文）。"""
    lower = text.lower()
    triggers = (
        "quote",
        "how much",
        "price",
        "cost",
        "estimate",
        "ballpark",
        "pricing",
        "多少钱",
        "报价",
        "价格",
    )
    return any(t in lower for t in triggers)


def _questionnaire_summary(req: RequirementState, *, bilingual: bool = False) -> str:
    """问卷确认摘要；Web 双语，WhatsApp 英文。"""
    q = req.as_questionnaire()
    if not q:
        return (
            "*已确认问卷 / Confirmed questionnaire*: (empty)"
            if bilingual
            else "Confirmed questionnaire: (empty)"
        )
    header = (
        "*已确认问卷 / Confirmed questionnaire*"
        if bilingual
        else "*Confirmed questionnaire*"
    )
    lines = [header]
    for key, value in q.items():
        lines.append(f"• {key}: {value}")
    lines.append(
        "可随时更正（例如「改成 2 bed turnkey」）。\n---\n"
        "Reply with corrections anytime (e.g. \"actually 2 bed turnkey\")."
        if bilingual
        else 'Reply with corrections anytime (e.g. "actually 2 bed turnkey").'
    )
    return "\n".join(lines)
