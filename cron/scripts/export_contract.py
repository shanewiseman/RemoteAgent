from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from remoteagent_cron.app import create_app  # noqa: E402
from remoteagent_cron.config import Settings  # noqa: E402

CONTRACT_PATH = PACKAGE_ROOT / "openapi.json"


def rendered_contract() -> str:
    app = create_app(
        Settings(
            environment="test",
            database_url="sqlite+aiosqlite:///:memory:",
            scheduler_enabled=False,
        )
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the cron internal OpenAPI contract")
    parser.add_argument(
        "--check", action="store_true", help="fail if the checked-in contract has drifted"
    )
    arguments = parser.parse_args()
    expected = rendered_contract()
    if arguments.check:
        try:
            current = CONTRACT_PATH.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != expected:
            print(f"cron OpenAPI contract is stale: {CONTRACT_PATH}", file=sys.stderr)
            return 1
        return 0
    CONTRACT_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
