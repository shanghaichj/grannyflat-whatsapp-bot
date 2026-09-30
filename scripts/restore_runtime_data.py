from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members: list[zipfile.ZipInfo] = []
    for member in archive.infolist():
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive path: {member.filename}")
        members.append(member)
    return members


def restore_backup(backup: Path, data_dir: Path, *, confirmed: bool) -> Path:
    if not confirmed:
        raise ValueError("restore requires --confirm")
    backup = backup.resolve(strict=True)
    data_dir = data_dir.resolve()
    if data_dir.parent == data_dir or len(data_dir.parts) < 3:
        raise ValueError("refusing broad data directory")
    rollback = data_dir.parent / (
        data_dir.name
        + "-before-restore-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    with zipfile.ZipFile(backup) as archive, tempfile.TemporaryDirectory(
        prefix="grannyflat-restore-"
    ) as temp_name:
        members = _safe_members(archive)
        manifest = json.loads(archive.read("backup-manifest.json"))
        temp = Path(temp_name)
        archive.extractall(temp, members=members)
        for item in manifest.get("files", []):
            staged = temp / item["path"]
            digest = hashlib.sha256(staged.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                raise ValueError(f"backup hash mismatch: {item['path']}")
        if data_dir.exists():
            shutil.copytree(data_dir, rollback)
        for item in manifest.get("files", []):
            staged = temp / item["path"]
            destination = data_dir / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, destination)
    return rollback


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore runtime data with a pre-restore copy")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    rollback = restore_backup(args.backup, args.data_dir, confirmed=args.confirm)
    print(f"Restored. Previous data copied to: {rollback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
