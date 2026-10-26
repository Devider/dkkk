"""Run S0.2 spike checks against live GigaChat and write a JSON report.

python scripts/spikes/s0_2_gigachat/run.py --checks C1,C3
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2] / "src")]

from checks import CHECKS

from aigw_service.config import APP_CONFIG


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks", default=",".join(CHECKS), help="comma-separated ids, e.g. C1,C3")
    parser.add_argument("--out", help="report path (default: results/<mode>-<timestamp>.json)")
    args = parser.parse_args()

    mode = APP_CONFIG.gigachat.auth_mode
    mode_name = mode.value if mode else "none"
    report = {
        "mode": mode_name,
        "model": APP_CONFIG.gigachat.model,
        "host": APP_CONFIG.gigachat.host,
        "started": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "results": [],
    }
    for check_id in [c.strip().upper() for c in args.checks.split(",") if c.strip()]:
        started = time.monotonic()
        print(f"{check_id} ...", end=" ", flush=True)
        try:
            result = await CHECKS[check_id]()
        except Exception as exc:
            result = {"check": check_id, "ok_rate": "crash", "error": type(exc).__name__, "message": str(exc)[:500]}
        result["seconds"] = round(time.monotonic() - started, 1)
        report["results"].append(result)
        print(result.get("ok_rate", result.get("skipped")), f"({result['seconds']} s)")

    out = (
        Path(args.out)
        if args.out
        else HERE / "results" / f"{mode_name}-{datetime.now(UTC).astimezone():%Y%m%d-%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"report: {out}")


if __name__ == "__main__":
    asyncio.run(main())
