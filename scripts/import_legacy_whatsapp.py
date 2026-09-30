"""Import an exported old-number workbook into its isolated SessionStore."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

from grannyflat_bot.session.store import Session, SessionStore


def clean(value: object) -> str:
    return str(value or "").strip()


def parse_time(value: object, fallback: str) -> str:
    text = clean(value)
    if not text:
        return fallback
    for fmt in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return fallback


def session_id_for(name: str, phone: str | None) -> str:
    if phone:
        digits = re.sub(r"\D", "", phone)
        prefix = "+" if phone.strip().startswith("+") else ""
        return f"legacy-wa-{prefix}{digits}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
    return f"legacy-contact-{digest}"


def load_phone_map(path: Path) -> dict[str, str]:
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    result: dict[str, str] = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        name, phone = clean(row[1]), clean(row[3])
        if name and phone:
            if name in result and result[name] != phone:
                raise ValueError(f"duplicate contact name with different phones: {name}")
            result[name] = phone
    return result


def import_workbook(export_path: Path, phone_path: Path, output_path: Path) -> dict[str, int]:
    phone_map = load_phone_map(phone_path)
    workbook = openpyxl.load_workbook(export_path, read_only=True, data_only=True)
    contact_rows = list(workbook["联系人"].iter_rows(min_row=2, values_only=True))
    messages: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in workbook["消息记录"].iter_rows(min_row=2, values_only=True):
        if row[0]:
            messages[clean(row[0])].append(row)

    if output_path.exists():
        output_path.unlink()
    store = SessionStore(output_path)
    imported_messages = 0
    matched_phones = 0
    base_time = datetime.now(timezone.utc).isoformat()

    for row in contact_rows:
        name = clean(row[1])
        if not name:
            continue
        phone = phone_map.get(name)
        if phone:
            matched_phones += 1
        session = Session(
            session_id=session_id_for(name, phone),
            channel="legacy_whatsapp",
            locale="original",
            display_name=name,
            name_manual=True,
            whatsapp_phone=phone,
            whatsapp_profile_name=name,
            contact_type="unknown",
            workflow_status="new",
            customer_status="new",
            guided_intake=False,
            created_at=base_time,
            updated_at=base_time,
        )
        history = []
        for msg in messages.get(name, []):
            direction = clean(msg[2])
            role = "user" if direction == "对方" else "assistant" if direction == "我" else "system"
            at = parse_time(msg[3], base_time)
            history.append({
                "role": role,
                "content": clean(msg[4]),
                "at": at,
                "source": "whatsapp_export",
                "source_direction": direction,
                "source_sequence": msg[1],
            })
        session.history = history
        if history:
            session.created_at = history[0]["at"]
            session.updated_at = history[-1]["at"]
            session.last_read_at = next(
                (m["at"] for m in reversed(history) if m["role"] == "user"), None
            )
        store.save(session, touch=False)
        imported_messages += len(history)

    return {
        "contacts": len(contact_rows),
        "messages": imported_messages,
        "matched_phones": matched_phones,
        "unmatched_phones": len(contact_rows) - matched_phones,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("phones", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(import_workbook(args.export, args.phones, args.output))
