"""Run S6.1 spike checks and write a JSON report.

python scripts/spikes/s6_1_stream/run.py --checks P1,P4
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT), str(ROOT / "src")]

from agent_checks import CHECKS as AGENT_CHECKS
from gigachat_checks import CHECKS as GIGACHAT_CHECKS
from http_checks import CHECKS as HTTP_CHECKS
from raw_stream import CHECKS as RAW_CHECKS

CHECKS = {**AGENT_CHECKS, **GIGACHAT_CHECKS, **RAW_CHECKS, **HTTP_CHECKS}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks", default=",".join(AGENT_CHECKS), help="comma-separated ids, e.g. P1,G1,H2")
    parser.add_argument("--out", help="report path (default: results/<checks>-<timestamp>.json)")
    args = parser.parse_args()
    ids = [c.strip().upper() for c in args.checks.split(",") if c.strip()]
    report = {"started": datetime.now(UTC).astimezone().isoformat(timespec="seconds"), "results": []}
    for check_id in ids:
        started = time.monotonic()
        print(f"{check_id} ...", end=" ", flush=True)
        try:
            result = await CHECKS[check_id]()
        except Exception as exc:  # pylint: disable=broad-exception-caught  # a crash is a result of the spike
            import traceback  # pylint: disable=import-outside-toplevel

            result = {"check": check_id, "crash": type(exc).__name__, "message": str(exc)[:800]}
            result["trace"] = traceback.format_exc()[-3000:]
        result["wall_s"] = round(time.monotonic() - started, 1)
        report["results"].append(result)
        print("crash" if "crash" in result else "done", f"({result['wall_s']} s)")
    stamp = f"{datetime.now(UTC).astimezone():%Y%m%d-%H%M%S}"
    out = Path(args.out) if args.out else HERE / "results" / f"{'_'.join(ids)}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"report: {out}")


if __name__ == "__main__":
    asyncio.run(main())
