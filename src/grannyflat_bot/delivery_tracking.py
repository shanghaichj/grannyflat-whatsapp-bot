"""Durable per-message delivery receipts, independent of conversation writes."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4

from grannyflat_bot.config import DATA_DIR

CALLBACK_URL = "https://ai.ausgrannyflat.com/webhooks/twilio/message-status"
DB = DATA_DIR / "message_receipts.sqlite3"
# "sending" 是本地占位状态，必须低于 Twilio 任何真实状态，保证 REST 返回/回调能覆盖它
RANK = {"sending": -1, "accepted": 0, "queued": 0, "sent": 2,
        "failed": 3, "undelivered": 3, "canceled": 3, "delivered": 4, "read": 5}

@contextmanager
def connection():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, message_at TEXT NOT NULL, sid TEXT UNIQUE, status TEXT NOT NULL, error TEXT)")
        yield db
        db.commit()
    finally:
        db.close()

def begin(session_id, message_at):
    key = uuid4().hex
    with connection() as db:
        db.execute("INSERT INTO receipts VALUES (?, ?, ?, NULL, 'sending', NULL)", (key, session_id, message_at))
    return key

def update(key, status, sid=None, error=None):
    if status not in RANK:
        return False
    with connection() as db:
        row = db.execute("SELECT * FROM receipts WHERE id=?", (key,)).fetchone()
        if not row or (row['sid'] and sid and row['sid'] != sid):
            return False
        # Callbacks can arrive before the REST response or out of order.
        if RANK[status] < RANK.get(row['status'], 0):
            return True
        db.execute("UPDATE receipts SET status=?, sid=COALESCE(sid, ?), error=? WHERE id=?", (status, sid, str(error)[:200] if error else None, key))
    return True

def fix_message_at(key, message_at):
    """模板类消息先建回执、发送后才入历史，回填 history 时间戳以便按消息对账。"""
    if not message_at:
        return False
    with connection() as db:
        row = db.execute("SELECT id FROM receipts WHERE id=?", (key,)).fetchone()
        if not row:
            return False
        db.execute("UPDATE receipts SET message_at=? WHERE id=?", (message_at, key))
    return True

def history_with_receipts(session):
    with connection() as db:
        rows = db.execute("SELECT * FROM receipts WHERE session_id=?", (session.session_id,)).fetchall()
    by_at = {row['message_at']: dict(row) for row in rows}
    return [{**item, **({'delivery_status': by_at[item['at']]['status'],
                        'delivery_error': by_at[item['at']]['error']} if item.get('at') in by_at else {})}
            for item in session.history]

async def send_tracked(channel, session, text=None, content_sid=None, message_at=None):
    from grannyflat_bot.channels.base import OutboundMessage
    key = begin(session.session_id, message_at or session.history[-1]['at'])
    callback = CALLBACK_URL + '?receipt=' + key
    try:
        phone = session.session_id.removeprefix('wa-')
        if content_sid:
            result = await channel.send_content_template(to=phone, content_sid=content_sid, status_callback=callback)
        else:
            result = await channel.send_text(OutboundMessage(session_id=session.session_id, text=text,
                metadata={'to': phone, 'status_callback': callback}))
        if not isinstance(result, dict) or not result.get('sid'):
            raise RuntimeError('Twilio did not return a message SID')
        update(key, result.get('status') or 'queued', result['sid'])
        return result
    except Exception as exc:
        update(key, 'failed', error=type(exc).__name__)
        raise

def install_callback(app):
    from fastapi import Request, HTTPException
    from fastapi.responses import Response
    from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel
    from grannyflat_bot.config import get_settings

    @app.post('/webhooks/twilio/message-status')
    async def receive_status(request: Request):
        settings = get_settings()
        form = {key: str(value) for key, value in (await request.form()).items()}
        query = request.url.query
        url = CALLBACK_URL + ('?' + query if query else '')
        if not TwilioWhatsAppChannel(settings).verify_signature(url, form, request.headers.get('X-Twilio-Signature', '')):
            raise HTTPException(403, 'Invalid signature')
        if form.get('AccountSid') != settings.twilio_account_sid:
            raise HTTPException(403, 'Invalid account')
        key = request.query_params.get('receipt', '')
        sid = form.get('MessageSid', '')
        if not sid:
            raise HTTPException(400, 'Missing MessageSid')
        update(key, form.get('MessageStatus', ''), sid, form.get('ErrorCode'))
        return Response(status_code=204)
