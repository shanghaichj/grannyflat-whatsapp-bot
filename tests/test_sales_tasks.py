from datetime import datetime, timedelta, timezone

from grannyflat_bot.sales_tasks import build_sales_tasks, task_summary
from grannyflat_bot.session.store import Session


NOW = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)


def test_tasks_include_unanswered_and_overdue_follow_up() -> None:
    session = Session(session_id="wa-+61400000100")
    session.add_message("user", "Can you confirm the price?")
    session.follow_up_due_at = (NOW - timedelta(hours=1)).isoformat()
    session.next_action = "Confirm approved package"

    tasks = build_sales_tasks([session], now=NOW)

    assert [task["kind"] for task in tasks] == ["reply_customer", "follow_up"]
    assert tasks[0]["priority"] == "urgent"
    assert tasks[1]["overdue"] is True


def test_closed_and_archived_contacts_create_no_tasks() -> None:
    closed = Session(session_id="closed", sales_stage="closed")
    closed.follow_up_due_at = (NOW - timedelta(days=1)).isoformat()
    archived = Session(session_id="archived", archived=True)
    archived.follow_up_due_at = (NOW - timedelta(days=1)).isoformat()

    assert build_sales_tasks([closed, archived], now=NOW) == []


def test_stale_cold_lead_is_review_only() -> None:
    session = Session(
        session_id="cold",
        created_at=(NOW - timedelta(days=10)).isoformat(),
        updated_at=(NOW - timedelta(days=10)).isoformat(),
        lead_temperature="cold",
    )
    session.history = [
        {
            "role": "assistant",
            "content": "Information sent",
            "at": (NOW - timedelta(days=10)).isoformat(),
        }
    ]

    tasks = build_sales_tasks([session], now=NOW)

    assert len(tasks) == 1
    assert tasks[0]["kind"] == "cold_lead_review"
    assert "不自动发消息" in tasks[0]["reason"]


def test_hot_lead_without_existing_follow_up_gets_priority_task() -> None:
    session = Session(session_id="hot", lead_temperature="hot")
    session.add_message("assistant", "Quote details sent")

    tasks = build_sales_tasks([session], now=NOW)

    assert len(tasks) == 1
    assert tasks[0]["kind"] == "hot_lead"
    assert tasks[0]["priority"] == "high"
    assert tasks[0]["title"] == "优先推进重点客户"


def test_non_target_contact_gets_no_proactive_follow_up_task() -> None:
    session = Session(
        session_id="non-target",
        lead_temperature="non_target",
        follow_up_due_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    session.add_message("assistant", "Information sent")

    assert build_sales_tasks([session], now=NOW) == []


def test_manual_meeting_is_exposed_to_crm_dashboard() -> None:
    session = Session(session_id="manual-meeting")
    session.next_action = "与客户举行方案会议"
    session.next_action_manual = True
    session.follow_up_due_at = (NOW + timedelta(days=2)).isoformat()
    session.follow_up_due_manual = True

    task = build_sales_tasks([session], now=NOW)[0]

    assert task["kind"] == "follow_up"
    assert task["title"] == "客户会议"
    assert task["reason"] == "与客户举行方案会议"
    assert task["manual"] is True
    assert task["action_type"] == "meeting"


def test_manual_next_action_without_time_is_still_visible() -> None:
    session = Session(session_id="manual-action")
    session.next_action = "准备新报价并电话联系客户"
    session.next_action_manual = True

    task = build_sales_tasks([session], now=NOW)[0]

    assert task["kind"] == "manual_action"
    assert task["due_at"] is None
    assert task["reason"] == "准备新报价并电话联系客户"
    assert task["manual"] is True


def test_task_summary_counts_priorities_and_kinds() -> None:
    session = Session(session_id="wa-+61400000101", escalated=True)
    session.add_message("user", "I need help")
    tasks = build_sales_tasks([session], now=NOW)

    summary = task_summary(tasks)

    assert summary["total"] == 2
    assert summary["urgent"] == 1
    assert summary["high"] == 1
    assert summary["by_kind"]["human_review"] == 1


def test_english_escalation_reason_is_explained_in_chinese() -> None:
    session = Session(session_id="pricing", escalated=True)
    session.escalation_reason = (
        "Pricing enquiry; final package figures and inclusions require confirmation."
    )
    session.add_message("assistant", "Let me confirm that for you")

    tasks = build_sales_tasks([session], now=NOW)

    review = next(task for task in tasks if task["kind"] == "human_review")
    assert "价格" in review["reason"]
    assert "人工确认" in review["reason"]
    assert "Pricing enquiry" not in review["reason"]
