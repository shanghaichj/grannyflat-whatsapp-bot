from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def release_files() -> list[Path]:
    files: list[Path] = []
    for relative in ("src", "tests", "docs", "scripts", "data/knowledge", "data/prompts"):
        root = ROOT / relative
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and not any(part.endswith(".egg-info") for part in path.parts)
            and path.name != "cooperation_and_costs.md"
            and path.name != "learned_from_agents.md"
            and ".bak" not in path.name
            and path.suffix not in {".pyc", ".sqlite3"}
        )
    for name in ("README.md", "requirements.txt", "pyproject.toml", ".gitignore", ".env.example"):
        path = ROOT / name
        if path.exists():
            files.append(path)
    return sorted(set(files))


def build_release(output: Path) -> dict[str, object]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "channel": "twilio_whatsapp",
        "requires_manual_production_approval": True,
        "files": [],
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in release_files():
            relative = source.relative_to(ROOT).as_posix()
            payload = source.read_bytes()
            archive.writestr(relative, payload)
            manifest["files"].append(
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        archive.writestr(
            "release-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a secret-free release candidate")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_release(args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
