from grannyflat_bot.config import Settings
from grannyflat_bot.preflight import run_preflight


def _production_settings(**overrides):
    values = {
        "app_environment": "production",
        "openai_api_key": "sk-test-placeholder-key",
        "mock_llm": False,
        "twilio_account_sid": "AC123",
        "twilio_auth_token": "auth-token",
        "twilio_whatsapp_from": "whatsapp:+61400000000",
        "twilio_validate_webhook": True,
        "twilio_inbound_webhook_url": "https://example.test/webhooks/twilio/whatsapp",
        "twilio_whatsapp_enabled": True,
        "admin_password": "a-long-password",
        "admin_session_secret": "x" * 32,
        "public_web_chat_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_preflight_accepts_safe_configuration(tmp_path):
    report = run_preflight(
        _production_settings(),
        sessions_db=tmp_path / "not-created.sqlite3",
    )

    assert report["ok"] is True
    assert report["ready_for_approval"] is True


def test_production_preflight_rejects_unsafe_configuration(tmp_path):
    report = run_preflight(
        _production_settings(
            twilio_validate_webhook=False,
            admin_password="short",
            public_web_chat_enabled=True,
        ),
        sessions_db=tmp_path / "not-created.sqlite3",
    )

    failed = {item["name"] for item in report["checks"] if not item["passed"]}
    assert report["ok"] is False
    assert {"twilio_signature", "admin_password", "web_chat_disabled"} <= failed


def test_qhc_gap_is_warning_not_invented_blocker(tmp_path):
    report = run_preflight(
        _production_settings(),
        sessions_db=tmp_path / "not-created.sqlite3",
    )

    qhc = next(item for item in report["checks"] if item["name"] == "qhc_source")
    assert qhc["required"] is False
