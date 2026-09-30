"""Deployment preflight checks. Never prints credential values."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from grannyflat_bot.config import DATA_DIR, Settings
from grannyflat_bot.knowledge.audit import audit_knowledge


def run_preflight(
    settings: Settings,
    *,
    sessions_db: Path | None = None,
    require_production: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, *, required: bool = True) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "required": required,
                "detail": detail,
            }
        )

    check(
        "environment",
        settings.production if require_production else True,
        "APP_ENVIRONMENT 必须是 production" if require_production else settings.app_environment,
    )
    check("twilio_configured", settings.twilio_configured, "Twilio 账号、发送号码和 REST 凭证齐全")
    check("twilio_enabled", settings.twilio_whatsapp_enabled, "Twilio WhatsApp 总开关已开启")
    check("twilio_signature", settings.twilio_validate_webhook, "入站 webhook 必须校验 Twilio 签名")
    check("twilio_auth_token", bool(settings.twilio_auth_token), "签名校验需要 Auth Token")
    check(
        "twilio_webhook_url",
        settings.twilio_inbound_webhook_url.startswith("https://")
        and settings.twilio_inbound_webhook_url.endswith(
            "/webhooks/twilio/whatsapp"
        ),
        "配置 Twilio Console 中完全一致的 HTTPS 入站 webhook URL",
    )
    check("live_llm", settings.use_live_llm, "生产环境不可使用 MOCK_LLM")
    check("admin_password", len(settings.admin_password) >= 12, "后台密码至少 12 个字符")
    check("admin_session_secret", len(settings.admin_session_secret) >= 32, "后台会话密钥至少 32 个字符")
    check("web_chat_disabled", not settings.public_web_chat_enabled, "Twilio-only 模式必须关闭公开网页聊天")

    knowledge = audit_knowledge()
    check("knowledge", knowledge["ok"], "客户知识库可读取且未混入内部成本文件")
    check(
        "qhc_source",
        bool(knowledge["coverage"].get("qhc_information")),
        "缺少经批准的 QHC 资料；系统不得自行补写",
        required=False,
    )

    db_path = sessions_db or (DATA_DIR / "sessions" / "sessions.sqlite3")
    db_result = _sqlite_integrity(db_path)
    check(
        "sessions_database",
        db_result in ("ok", "not_created"),
        f"会话数据库完整性：{db_result}",
    )

    required_failures = [
        item for item in checks if item["required"] and not item["passed"]
    ]
    return {
        "ok": not required_failures,
        "ready_for_approval": not required_failures,
        "checks": checks,
        "warnings": knowledge["warnings"],
        "knowledge": {
            "source_count": knowledge["source_count"],
            "chunk_count": knowledge["chunk_count"],
            "coverage": knowledge["coverage"],
        },
    }


def production_config_errors(settings: Settings) -> list[str]:
    report = run_preflight(settings, require_production=True)
    return [
        str(item["name"])
        for item in report["checks"]
        if item["required"] and not item["passed"]
    ]


def _sqlite_integrity(path: Path) -> str:
    if not path.exists():
        return "not_created"
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        return str(row[0] if row else "unknown")
    except sqlite3.Error as exc:
        return f"error:{type(exc).__name__}"
