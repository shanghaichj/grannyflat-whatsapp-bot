import json
import sqlite3
import zipfile
from pathlib import Path

from scripts.backup_runtime_data import create_backup
from scripts.build_release import build_release
from scripts.restore_runtime_data import restore_backup
from scripts.verify_release import verify_release


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    data = tmp_path / "runtime-data"
    (data / "knowledge").mkdir(parents=True)
    (data / "prompts").mkdir(parents=True)
    (data / "knowledge" / "faq.md").write_text("approved facts", encoding="utf-8")
    (data / "prompts" / "extra.md").write_text("approved script", encoding="utf-8")
    db = data / "sessions" / "sessions.sqlite3"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('before')")
    connection.commit()
    connection.close()

    backup = tmp_path / "backup.zip"
    manifest = create_backup(data, backup)
    (data / "knowledge" / "faq.md").write_text("changed", encoding="utf-8")
    rollback = restore_backup(backup, data, confirmed=True)

    assert backup.exists()
    assert len(manifest["files"]) == 3
    assert (data / "knowledge" / "faq.md").read_text(encoding="utf-8") == "approved facts"
    assert (rollback / "knowledge" / "faq.md").read_text(encoding="utf-8") == "changed"
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "before"
    finally:
        connection.close()


def test_restore_requires_explicit_confirmation(tmp_path: Path) -> None:
    backup = tmp_path / "missing.zip"
    try:
        restore_backup(backup, tmp_path / "data", confirmed=False)
    except ValueError as exc:
        assert "--confirm" in str(exc)
    else:
        raise AssertionError("restore should require confirmation")


def test_release_archive_excludes_secrets_and_runtime_data(tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    manifest = build_release(output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        embedded = json.loads(archive.read("release-manifest.json"))

    assert ".env" not in names
    assert not any(name.endswith(".sqlite3") for name in names)
    assert not any("data/internal" in name for name in names)
    assert "data/knowledge/cooperation_and_costs.md" not in names
    assert "data/prompts/learned_from_agents.md" not in names
    assert not any("data/pricing" in name for name in names)
    assert not any(".egg-info/" in name for name in names)
    assert not any(".bak" in name for name in names)
    assert "src/grannyflat_bot/app.py" in names
    assert embedded["requires_manual_production_approval"] is True
    assert len(manifest["files"]) > 10
    assert verify_release(output)["ok"] is True


def test_release_verifier_detects_tampered_payload(tmp_path: Path) -> None:
    original = tmp_path / "release-original.zip"
    output = tmp_path / "release-tampered.zip"
    build_release(original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            payload = b"tampered" if item.filename == "README.md" else source.read(item)
            target.writestr(item, payload)

    report = verify_release(output)

    assert report["ok"] is False
    assert any(error.startswith("hash_mismatch:README.md") for error in report["errors"])
