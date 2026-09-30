"""从客户消息抽取报价问卷补丁，并生成对客英文 reply。"""

from __future__ import annotations

from typing import Any

from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.llm.client import LLMClient
from grannyflat_bot.llm.prompts import ReplyLocale, build_structured_system


class RequirementExtractor:
    """每轮用 LLM 结构化输出：更新问卷 + 起草回复 + 是否转人工。"""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    def extract_turn(
        self,
        *,
        user_text: str,
        requirements: RequirementState,
        knowledge: str,
        history: list[dict[str, str]] | None = None,
        locale: ReplyLocale = "en",
    ) -> dict[str, Any]:
        system = build_structured_system(
            requirements_json=requirements.model_dump_json(
                exclude={"field_sources", "field_confidence"}
            ),
            knowledge=knowledge,
            locale=locale,
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        if history:
            # 保留最近几轮，保证指代连贯
            messages.extend(
                {"role": item["role"], "content": item["content"]}
                for item in history[-6:]
                if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)
            )
        messages.append({"role": "user", "content": user_text})

        try:
            data = self.llm.chat_json(messages)
        except Exception:
            # API/JSON failure: honest handoff, do not spam quote intake questions
            from grannyflat_bot.intake.guided_flow import format_intake_complete

            unsure = format_intake_complete(locale=locale)
            return {
                "reply": unsure,
                "requirement_patch": {},
                "offer_quote": False,
                "escalate_to_human": True,
                "escalation_reason": "llm_extract_failed",
                "crm_summary": None,
                "next_action": None,
                "follow_up_in_hours": None,
            }

        patch = data.get("requirement_patch") or {}
        if not isinstance(patch, dict):
            patch = {}

        from grannyflat_bot.intake.guided_flow import format_intake_complete

        fallback = format_intake_complete(locale=locale)

        follow_up_in_hours = data.get("follow_up_in_hours")
        try:
            follow_up_in_hours = int(follow_up_in_hours)
            if not 1 <= follow_up_in_hours <= 720:
                follow_up_in_hours = None
        except (TypeError, ValueError):
            follow_up_in_hours = None

        return {
            "reply": str(data.get("reply") or "").strip() or fallback,
            "requirement_patch": patch,
            "offer_quote": bool(data.get("offer_quote")),
            "escalate_to_human": bool(data.get("escalate_to_human")),
            "escalation_reason": data.get("escalation_reason"),
            "crm_summary": str(data.get("crm_summary") or "").strip() or None,
            "next_action": str(data.get("next_action") or "").strip() or None,
            "follow_up_in_hours": follow_up_in_hours,
        }
