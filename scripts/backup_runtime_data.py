from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data"


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def create_backup(data_dir: Path, output: Path) -> dict[str, object]:
    data_dir = data_dir.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    included: list[Path] = []
    for relative in ("knowledge", "prompts"):
        root = data_dir / relative
        if root.exists():
            included.extend(path for path in root.rglob("*") if path.is_file() and ".bak" not in path.name)
    # Back up every SQLite database under data/, including legacy-number data.
    # The SQLite backup API produces a consistent snapshot while the service is
    # still running, unlike copying a live database file byte-for-byte.
    included.extend(path for path in data_dir.rglob("*.sqlite3") if path.is_file())
    included = sorted(set(included))

    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_root": str(data_dir),
        "files": [],
    }
    with tempfile.TemporaryDirectory(prefix="grannyflat-backup-") as temp_name:
        temp = Path(temp_name)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in included:
                relative = source.relative_to(data_dir)
                staged = source
                if source.suffix == ".sqlite3":
                    staged = temp / relative
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    _sqlite_backup(source, staged)
                payload = staged.read_bytes()
                archive.writestr(relative.as_posix(), payload)
                manifest["files"].append(
                    {
                        "path": relative.as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            archive.writestr(
                "backup-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a consistent runtime-data backup")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = create_backup(args.data_dir, args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
