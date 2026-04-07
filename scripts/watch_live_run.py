from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(E2E_ROOT))

from scripts._support.run_status import (
    TERMINAL_RUN_STATUSES,
    format_run_status_line,
    resolve_status_path,
)


def _load_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "invalid_status_file", "error": str(exc), "artifacts": {"status_file": str(path)}}


async def _follow_status(path: Path, *, interval_s: float) -> int:
    last_payload = ""
    while True:
        payload = _load_status(path)
        current = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if current != last_payload:
            print(format_run_status_line(payload), flush=True)
            last_payload = current
        if str(payload.get("status") or "").strip() in TERMINAL_RUN_STATUSES:
            return 0
        await asyncio.sleep(max(0.2, float(interval_s)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch a real-flow run_status.json progress stream.")
    parser.add_argument("path", help="Output directory or run_status.json path")
    parser.add_argument("--follow", action="store_true", default=False, help="Poll until the run reaches a terminal status")
    parser.add_argument("--interval-s", type=float, default=1.5, help="Polling interval when --follow is enabled")
    parser.add_argument("--json", action="store_true", default=False, help="Print the full status payload as JSON")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    path = resolve_status_path(Path(args.path))
    if args.follow:
        return await _follow_status(path, interval_s=args.interval_s)
    payload = _load_status(path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_run_status_line(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
