"""Jobs for EngineGate tests: top-level, stdlib only, so a spawned pool worker imports them fast."""

import os
import subprocess
import time


def interval(seconds: float) -> tuple[float, float, int]:
    """Sleep and report (start, end, worker pid)."""
    start = time.monotonic()
    time.sleep(seconds)
    return start, time.monotonic(), os.getpid()


def hang_with_child(pid_file: str) -> None:
    """Stand-in for a hung recalculation: start a child (like soffice) and never return."""
    child = subprocess.Popen(["sleep", "300"])
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(child.pid))
    time.sleep(300)


def crash() -> None:
    os._exit(1)


def echo(value: object) -> object:
    return value
