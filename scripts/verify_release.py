from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = "release-manifest.json"


def verify_release(archive_path: Path) -> dict[str, Any]:
    """Verify archive paths and every payload hash without extracting the release."""
    errors: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if MANIFEST_NAME not in names:
            return {"ok": False, "files_checked": 0, "errors": ["manifest_missing"]}

        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                errors.append(f"unsafe_path:{name}")

        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ok": False, "files_checked": 0, "errors": ["manifest_invalid"]}

        entries = manifest.get("files", [])
        if not isinstance(entries, list):
            return {"ok": False, "files_checked": 0, "errors": ["manifest_files_invalid"]}

        expected_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                errors.append("manifest_entry_invalid")
                continue
            name = str(entry.get("path", ""))
            expected_names.add(name)
            if name not in names:
                errors.append(f"missing:{name}")
                continue
            payload = archive.read(name)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != entry.get("sha256"):
                errors.append(f"hash_mismatch:{name}")
            if len(payload) != entry.get("bytes"):
                errors.append(f"size_mismatch:{name}")

        unexpected = set(names) - expected_names - {MANIFEST_NAME}
        errors.extend(f"unexpected:{name}" for name in sorted(unexpected))

    return {
        "ok": not errors,
        "files_checked": len(expected_names),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a release candidate archive")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    report = verify_release(args.archive)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
