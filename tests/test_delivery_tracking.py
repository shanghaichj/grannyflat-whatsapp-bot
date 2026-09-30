"""消息送达追踪（delivery_tracking）单元测试。"""
import asyncio
from types import SimpleNamespace

import pytest

from grannyflat_bot import delivery_tracking as dt
from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel


@pytest.fixture()
def receipts_db(tmp_path, monkeypatch):
    """把回执库重定向到临时目录，避免污染真实 data/。"""
    path = tmp_path / "receipts.sqlite3"
    monkeypatch.setattr(dt, "DB", path)
    return path


def _session(session_id="wa-+61400000000", history=None):
    return SimpleNamespace(
        session_id=session_id,
        history=history if history is not None else [
            {"role": "assistant", "content": "hi", "at": "2026-09-25T01:00:00+00:00"}
        ],
    )


def test_begin_creates_sending_receipt(receipts_db):
    key = dt.begin("wa-1", "2026-09-25T00:00:00+00:00")
    assert key
    view = dt.history_with_receipts(_session("wa-1", []))
    assert view == []  # history 为空不报错
    merged = dt.history_with_receipts(
        _session("wa-1", [{"role": "assistant", "content": "a", "at": "2026-09-25T00:00:00+00:00"}])
    )
    assert merged[0]["delivery_status"] == "sending"


def test_update_advances_status_and_rejects_downgrade(receipts_db):
    key = dt.begin("wa-1", "T0")
    assert dt.update(key, "sent", sid="SM1")
    assert dt.update(key, "delivered", sid="SM1")
    # 乱序回调：sent 不能再覆盖 delivered
    assert dt.update(key, "sent", sid="SM1") is True
    merged = dt.history_with_receipts(_session("wa-1", [{"role": "assistant", "content": "a", "at": "T0"}]))
    assert merged[0]["delivery_status"] == "delivered"
    # 未知状态与 SID 对不上都拒绝
    assert dt.update(key, "bogus") is False
    assert dt.update(key, "read", sid="SM_OTHER") is False


def test_failed_records_error(receipts_db):
    key = dt.begin("wa-1", "T0")
    assert dt.update(key, "failed", error="SomeError")
    merged = dt.history_with_receipts(_session("wa-1", [{"role": "assistant", "content": "a", "at": "T0"}]))
    assert merged[0]["delivery_status"] == "failed"
    assert "SomeError" in (merged[0].get("delivery_error") or "")


def test_fix_message_at_links_receipt_to_history(receipts_db):
    key = dt.begin("wa-1", message_at="")
    assert not dt.history_with_receipts(_session("wa-1", [{"role": "assistant", "content": "tpl", "at": "TPL"}]))[0].get("delivery_status")
    assert dt.fix_message_at(key, "TPL")
    merged = dt.history_with_receipts(_session("wa-1", [{"role": "assistant", "content": "tpl", "at": "TPL"}]))
    assert merged[0]["delivery_status"] == "sending"
    assert dt.fix_message_at("no-such-key", "TPL") is False


class _FakeChannel:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_text(self, message):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("text", message.metadata.get("status_callback")))
        return {"sid": "SM9", "status": "queued"}

    async def send_content_template(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("tpl", kwargs.get("status_callback")))
        return {"sid": "SM10", "status": "accepted"}


def test_send_tracked_text_success(receipts_db):
    ch = _FakeChannel()
    result = asyncio.run(dt.send_tracked(ch, _session(), text="hello", message_at="2026-09-25T01:00:00+00:00"))
    assert result["sid"] == "SM9"
    kind, callback = ch.calls[0]
    assert kind == "text"
    assert callback.startswith(dt.CALLBACK_URL + "?receipt=")
    merged = dt.history_with_receipts(_session(history=[
        {"role": "assistant", "content": "hello", "at": "2026-09-25T01:00:00+00:00"}]))
    assert merged[0]["delivery_status"] == "queued"
    assert ch.calls[0][1].startswith(dt.CALLBACK_URL)


def test_send_tracked_template_success(receipts_db):
    ch = _FakeChannel()
    asyncio.run(dt.send_tracked(ch, _session(), content_sid="HXabc", message_at="T1"))
    kind, callback = ch.calls[0]
    assert kind == "tpl"
    assert callback.startswith(dt.CALLBACK_URL + "?receipt=")


def test_send_tracked_marks_failed_on_error(receipts_db):
    ch = _FakeChannel(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(dt.send_tracked(ch, _session(), text="hi", message_at="T1"))
    merged = dt.history_with_receipts(_session(history=[{"role": "assistant", "content": "a", "at": "T1"}]))
    assert merged[0]["delivery_status"] == "failed"


def test_build_twiml_status_callback_escaped():
    ch = TwilioWhatsAppChannel()
    xml = ch.build_twiml("hi", status_callback='https://x.com/cb?receipt="k"&a=1')
    assert 'statusCallback="https://x.com/cb?receipt=&quot;k&quot;&amp;a=1"' in xml
    # 无回调时保持原样（兼容旧行为）
    assert "<Message>" in ch.build_twiml("hi")
