from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from grannyflat_bot.config import get_settings
from grannyflat_bot.session.store import Session

# 问卷字段 → 中文标签（写入 MD / Excel）
FIELD_LABELS_ZH: dict[str, str] = {
    "state": "所在州",
    "suburb_or_lga": "Suburb / LGA",
    "lot_size_m2": "地块面积 (m²)",
    "approx_floor_area_m2": "目标建筑面积 (m²)",
    "bedrooms": "卧室数 (0=studio)",
    "bathrooms": "浴室数",
    "delivery_type": "交付方式",
    "include_council": "含许可协助",
    "include_design": "含设计",
    "include_foundations": "含基础",
    "include_services": "含水电接驳",
    "timeline": "工期偏好",
    "budget_aud_min": "预算下限 (AUD)",
    "budget_aud_max": "预算上限 (AUD)",
    "contact_name": "联系人",
    "contact_phone": "电话",
    "contact_email": "邮箱",
    "notes": "备注",
}


@dataclass
class SessionExportPaths:
    session_id: str
    profile_md: Path
    quote_xlsx: Path
    transcript_json: Path
    exported_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "profile_md": str(self.profile_md),
            "quote_xlsx": str(self.quote_xlsx),
            "transcript_json": str(self.transcript_json),
            "exported_at": self.exported_at,
        }


class SessionExporter:
    """生成 MD 用户画像、Excel 数值报价、JSON 全量聊天记录。"""

    def __init__(self, export_root: Path | None = None) -> None:
        settings = get_settings()
        self.export_root = export_root or settings.session_export_dir
        self.export_root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("\\", "_")
        return self.export_root / safe

    def export_session(
        self,
        session: Session,
        *,
        channel: str = "web",
        locale: str = "bilingual",
    ) -> SessionExportPaths:
        out_dir = self.session_dir(session.session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        exported_at = datetime.now(timezone.utc).isoformat()

        profile_path = out_dir / "user_profile.md"
        xlsx_path = out_dir / "quote_summary.xlsx"
        json_path = out_dir / "transcript.json"

        profile_path.write_text(
            self.build_profile_md(session, channel=channel, locale=locale, exported_at=exported_at),
            encoding="utf-8",
        )
        self.write_quote_xlsx(session, xlsx_path, exported_at=exported_at)
        json_path.write_text(
            json.dumps(
                self.build_transcript(session, channel=channel, locale=locale, exported_at=exported_at),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        paths = SessionExportPaths(
            session_id=session.session_id,
            profile_md=profile_path,
            quote_xlsx=xlsx_path,
            transcript_json=json_path,
            exported_at=exported_at,
        )
        session.exports = paths.to_dict()
        session.channel = channel
        session.locale = locale
        return paths

    def build_profile_md(
        self,
        session: Session,
        *,
        channel: str,
        locale: str,
        exported_at: str,
    ) -> str:
        req = session.requirements
        q = req.as_questionnaire()
        lines = [
            "# 客户画像 / User profile",
            "",
            f"- **Session**: `{session.session_id}`",
            f"- **Channel**: {channel}",
            f"- **Locale**: {locale}",
            f"- **Exported (UTC)**: {exported_at}",
            f"- **Messages**: {len(session.history)}",
            "",
            "## 需求摘要 / Requirements",
            "",
        ]
        if not q:
            lines.append("_（暂无结构化字段 / no structured fields yet）_")
        else:
            lines.append("| 字段 Field | 值 Value |")
            lines.append("|---|---|")
            for key, value in q.items():
                label = FIELD_LABELS_ZH.get(key, key)
                lines.append(f"| {label} (`{key}`) | {value} |")

        missing = req.missing_fields(["state", "bedrooms", "delivery_type"])
        if missing:
            lines.extend(["", "## 待补齐 / Missing for quote", ""])
            lines.append(", ".join(missing))

        if session.last_quote:
            qd = session.last_quote
            lines.extend(
                [
                    "",
                    "## 最新示意报价 / Latest indicative quote",
                    "",
                    f"- **Can quote**: {qd.get('can_quote')}",
                    f"- **Subtotal (mid)**: {qd.get('subtotal')} {qd.get('currency', 'AUD')}",
                    f"- **Range**: {qd.get('range_low')} – {qd.get('range_high')}",
                ]
            )
            if qd.get("disclaimer"):
                lines.extend(["", f"> {qd['disclaimer']}"])

        lines.extend(["", "## 备注 / Notes", ""])
        if req.notes:
            lines.append(req.notes)
        else:
            lines.append("_（无 / none）_")

        return "\n".join(lines) + "\n"

    def build_transcript(
        self,
        session: Session,
        *,
        channel: str,
        locale: str,
        exported_at: str,
    ) -> dict[str, Any]:
        messages = [
            {"index": i, "role": m["role"], "content": m["content"]}
            for i, m in enumerate(session.history)
        ]
        return {
            "session_id": session.session_id,
            "exported_at": exported_at,
            "channel": channel,
            "locale": locale,
            "message_count": len(messages),
            "questionnaire": session.requirements.as_questionnaire(),
            "requirements": session.requirements.model_dump(mode="json"),
            "quote": session.last_quote,
            "messages": messages,
            "visualization": {
                "timeline": messages,
                "questionnaire_keys": list(session.requirements.as_questionnaire().keys()),
            },
        }

    def write_quote_xlsx(
        self,
        session: Session,
        path: Path,
        *,
        exported_at: str,
    ) -> None:
        wb = Workbook()
        bold = Font(bold=True)

        # Sheet 1: questionnaire numbers
        ws1 = wb.active
        ws1.title = "Questionnaire"
        ws1.append(["Field", "Label (ZH)", "Value"])
        for cell in ws1[1]:
            cell.font = bold
        req_data = session.requirements.model_dump(mode="json")
        for key in FIELD_LABELS_ZH:
            val = req_data.get(key)
            if val is not None and val != "":
                ws1.append([key, FIELD_LABELS_ZH[key], val])
        for key, val in req_data.items():
            if key in FIELD_LABELS_ZH or key.startswith("field_"):
                continue
            if val is not None and val != "":
                ws1.append([key, key, val])

        # Sheet 2: quote line items
        ws2 = wb.create_sheet("Quote")
        ws2.append(["Exported (UTC)", exported_at])
        ws2.append(["Session", session.session_id])
        ws2.append([])
        if not session.last_quote:
            ws2.append(["Status", "No quote yet"])
        else:
            q = session.last_quote
            ws2.append(["Currency", q.get("currency", "AUD")])
            ws2.append(["Can quote", q.get("can_quote")])
            ws2.append(["Subtotal (mid)", q.get("subtotal")])
            ws2.append(["Range low", q.get("range_low")])
            ws2.append(["Range high", q.get("range_high")])
            ws2.append([])
            ws2.append(["Line item", "Amount (AUD)"])
            for cell in ws2[ws2.max_row]:
                cell.font = bold
            for item in q.get("line_items") or []:
                ws2.append([item.get("label"), item.get("amount_aud")])

        # Sheet 3: chat log
        ws3 = wb.create_sheet("ChatLog")
        ws3.append(["Index", "Role", "Content"])
        for cell in ws3[1]:
            cell.font = bold
        for i, msg in enumerate(session.history):
            ws3.append([i, msg.get("role"), msg.get("content")])

        wb.save(path)
