"""Read-only sales task projection built from CRM session state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def build_sales_tasks(
    sessions: Iterable[Any], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tasks: list[dict[str, Any]] = []
    for session in sessions:
        if session.archived or session.sales_stage == "closed":
            continue

        latest = session.last_message_entry()
        latest_role = str((latest or {}).get("role") or "")
        last_activity = _parse_datetime(session.last_activity_at())
        due = _parse_datetime(session.follow_up_due_at)

        if latest_role == "user":
            tasks.append(
                _task(
                    session,
                    kind="reply_customer",
                    priority="urgent",
                    title="回复客户最新消息",
                    reason="客户最后发言后尚无客服或机器人回复",
                    due_at=session.updated_at,
                )
            )

        if session.escalated:
            tasks.append(
                _task(
                    session,
                    kind="human_review",
                    priority="high",
                    title="人工检查复杂问题",
                    reason=_reason_zh(
                        session.escalation_reason,
                        kind="human_review",
                    ),
                    due_at=session.updated_at,
                )
            )

        if due and session.lead_temperature != "non_target":
            overdue = due <= current
            action_type = _action_type(session.next_action)
            tasks.append(
                _task(
                    session,
                    kind="follow_up",
                    priority="high" if overdue else "normal",
                    title=(
                        "客户会议"
                        if action_type == "meeting"
                        else ("跟进客户" if not overdue else "跟进已逾期")
                    ),
                    reason=_reason_zh(session.next_action, kind="follow_up"),
                    due_at=due.isoformat(),
                    overdue=overdue,
                    manual=bool(session.next_action_manual or session.follow_up_due_manual),
                    action_type=action_type,
                )
            )

        if session.next_action_manual and session.next_action and not due:
            tasks.append(
                _task(
                    session,
                    kind="manual_action",
                    priority="normal",
                    title="人工安排的下一步行动",
                    reason=_reason_zh(session.next_action, kind="follow_up"),
                    manual=True,
                    action_type=_action_type(session.next_action),
                )
            )

        if session.lead_temperature == "hot" and not any(
            task["session_id"] == session.session_id
            and task["kind"] in ("reply_customer", "follow_up")
            for task in tasks
        ):
            tasks.append(
                _task(
                    session,
                    kind="hot_lead",
                    priority="high",
                    title="优先推进重点客户",
                    reason=_reason_zh(session.next_action, kind="hot_lead"),
                )
            )

        if (
            session.lead_temperature == "cold"
            and session.has_real_message()
            and last_activity
            and last_activity <= current - timedelta(days=7)
            and not due
        ):
            tasks.append(
                _task(
                    session,
                    kind="cold_lead_review",
                    priority="low",
                    title="复核长期未推进线索",
                    reason="超过 7 天没有新对话；仅建议人工判断，不自动发消息",
                )
            )

    tasks.sort(
        key=lambda item: (
            PRIORITY_ORDER.get(item["priority"], 9),
            item.get("due_at") or "9999",
            item.get("session_id") or "",
        )
    )
    return tasks


def task_summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(tasks),
        "overdue": sum(1 for task in tasks if task.get("overdue")),
        "urgent": sum(1 for task in tasks if task.get("priority") == "urgent"),
        "high": sum(1 for task in tasks if task.get("priority") == "high"),
        "by_kind": {
            kind: sum(1 for task in tasks if task.get("kind") == kind)
            for kind in (
                "reply_customer",
                "human_review",
                "follow_up",
                "manual_action",
                "hot_lead",
                "cold_lead_review",
            )
        },
    }


def _task(
    session: Any,
    *,
    kind: str,
    priority: str,
    title: str,
    reason: str,
    due_at: str | None = None,
    overdue: bool = False,
    manual: bool = False,
    action_type: str = "follow_up",
) -> dict[str, Any]:
    return {
        "id": f"{kind}:{session.session_id}",
        "session_id": session.session_id,
        "kind": kind,
        "priority": priority,
        "title": title,
        "reason": reason,
        "due_at": due_at,
        "overdue": overdue,
        "manual": manual,
        "action_type": action_type,
    }


def _action_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    meeting_terms = (
        "meeting", "appointment", "site visit", "video call", "zoom", "teams",
        "会议", "会面", "预约", "拜访", "现场见面", "视频通话",
    )
    return "meeting" if any(term in text for term in meeting_terms) else "follow_up"


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reason_zh(value: Any, *, kind: str) -> str:
    """Return a concise Chinese operator explanation for CRM task cards."""
    text = str(value or "").strip()
    if text and any("\u4e00" <= char <= "\u9fff" for char in text):
        return text

    lowered = text.lower()
    if kind == "human_review":
        if any(word in lowered for word in ("price", "pricing", "quote", "quotation", "cost", "package")):
            return "客户正在询问价格或报价；最终金额及包含项目需要客服人工确认。"
        if any(word in lowered for word in ("council", "approval", "certification", "occupancy", "compliance", "regulation")):
            return "客户正在询问审批、认证或合规事项，需要客服人工确认。"
        if any(word in lowered for word in ("design", "floor plan", "layout", "bedroom", "bathroom", "size")):
            return "客户正在询问户型或设计细节，需要客服查看对话并人工确认。"
        if any(word in lowered for word in ("delivery", "shipping", "transport", "container")):
            return "客户正在询问运输或交付安排，需要客服人工确认。"
        if any(word in lowered for word in ("builder", "installation", "construction", "site")):
            return "客户正在询问施工、安装或现场条件，需要客服人工确认。"
        return "该问题超出机器人可确认范围，需要客服查看对话并人工处理。"

    if kind == "follow_up":
        return "按计划跟进客户，并在对话中确认下一步安排。"
    if kind == "hot_lead":
        return "该客户当前属于重点客户，请进入对话确认需求并推进下一步。"
    return text or "请进入客户对话查看并处理。"
