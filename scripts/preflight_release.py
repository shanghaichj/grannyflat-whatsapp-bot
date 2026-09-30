from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from grannyflat_bot.config import get_settings  # noqa: E402
from grannyflat_bot.preflight import run_preflight  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run secret-safe release preflight")
    parser.add_argument("--development", action="store_true", help="do not require APP_ENVIRONMENT=production")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()

    report = run_preflight(
        get_settings(), require_production=not args.development
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            marker = "PASS" if item["passed"] else ("WARN" if not item["required"] else "FAIL")
            print(f"[{marker}] {item['name']}: {item['detail']}")
        for warning in report["warnings"]:
            print(f"[WARN] knowledge: {warning}")
        print("READY" if report["ready_for_approval"] else "NOT READY")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
