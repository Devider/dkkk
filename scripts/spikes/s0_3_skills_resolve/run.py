"""Run S0.3 spike checks and write a JSON report.

python scripts/spikes/s0_3_skills_resolve/run.py --checks K1,K2
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


def load_checks() -> dict:
    from skills_checks import CHECKS as skills  # pylint: disable=import-outside-toplevel

    checks = dict(skills)
    try:
        from resolve_checks import CHECKS as resolve  # pylint: disable=import-outside-toplevel

        checks.update(resolve)
    except ImportError:
        pass
    return checks


async def main() -> None:
    checks = load_checks()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks", default=",".join(checks), help="comma-separated ids, e.g. K1,R1")
    parser.add_argument("--out", help="report path (default: results/<timestamp>.json)")
    args = parser.parse_args()

    now = datetime.now(UTC).astimezone()
    report = {"started": now.isoformat(timespec="seconds"), "results": []}
    for check_id in [c.strip().upper() for c in args.checks.split(",") if c.strip()]:
        started = time.monotonic()
        print(f"{check_id} ...", end=" ", flush=True)
        try:
            result = await checks[check_id]()
        except Exception as exc:
            result = {"check": check_id, "ok_rate": "crash", "error": type(exc).__name__, "message": str(exc)[:800]}
        result["seconds"] = round(time.monotonic() - started, 1)
        report["results"].append(result)
        print(result.get("ok_rate"), f"({result['seconds']} s)", flush=True)

    out = Path(args.out) if args.out else HERE / "results" / f"{now:%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"report: {out}")


if __name__ == "__main__":
    asyncio.run(main())
