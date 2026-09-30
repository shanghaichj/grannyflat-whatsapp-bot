from pathlib import Path

from grannyflat_bot.session.store import SessionStore


def test_claim_inbound_message_is_atomic_and_durable(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite3"
    first_store = SessionStore(db_path)

    assert first_store.claim_inbound_message("SM123", "wa-+61411111111") is True
    assert first_store.claim_inbound_message("SM123", "wa-+61411111111") is False

    # A new store simulates another worker or a service restart.
    second_store = SessionStore(db_path)
    assert second_store.claim_inbound_message("SM123", "wa-+61411111111") is False


def test_failed_inbound_claim_can_be_retried(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")

    assert store.claim_inbound_message("SM-retry", "wa-+61411111111") is True
    store.release_inbound_message("SM-retry")
    assert store.claim_inbound_message("SM-retry", "wa-+61411111111") is True


def test_missing_sid_keeps_legacy_callers_working(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")

    assert store.claim_inbound_message("", "wa-+61411111111") is True
    assert store.claim_inbound_message("", "wa-+61411111111") is True
