#!/usr/bin/env python3
"""
Experiment orchestrator for running tests across multiple Excel models.

Scans real_models/ for .xlsx files, matches them with query files in test_data/
by hash, then runs scripts/new_graph_test.py sequentially for each (model, query) pair.

Usage:
    python scripts/run_experiments.py --run-name my_baseline
    python scripts/run_experiments.py --run-name dry_run --dry-run
    python scripts/run_experiments.py --run-name test --models-dir /path --queries-dir /path

Output structure:
    experiments/{run_name}/
    ├── orchestrator.log            # full log (stdout+subprocess output)
    ├── ema_{hash}_unified_results.csv
    ├── ift_{hash}_unified_results.csv
    ├── ...
    └── summary.csv
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
NEW_GRAPH_TEST = PROJECT_ROOT / "scripts" / "new_graph_test.py"
DEFAULT_MODELS_DIR = PROJECT_ROOT / "real_models"
DEFAULT_QUERIES_DIR = PROJECT_ROOT / "test_data"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "experiments"


# ================================================================
# Logging utilities
# ================================================================

class LogWriter:
    """Dual-writer: writes to both a file and stdout."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115

    def write(self, text: str) -> None:
        self.fh.write(text)
        self.fh.flush()
        sys.stdout.write(text)

    def flush(self) -> None:  # noqa: A003
        self.fh.flush()
        sys.stdout.flush()

    def close(self) -> None:
        self.fh.close()


def _log_section(log: LogWriter, title: str) -> None:
    """Print a section header."""
    header = f"\n{'='*80}\n{title}\n{'='*80}\n"
    log.write(header)


def _log_entry(log: LogWriter, prefix: str, message: str) -> None:
    """Log a timestamped entry."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    log.write(f"[{ts}] [{prefix}] {message}\n")


# ================================================================
# Scan & discovery
# ================================================================

def _extract_hash(filename: str) -> str:
    """Extract hash from filename.

    Models: '2poj431skazt_depersonalized.xlsx' → '2poj431skazt'
    Queries: 'analyze_excel_model_2poj431skazt_depersonalized.csv' → '2poj431skazt'
    """
    stem = filename.replace(".xlsx", "").replace(".csv", "")

    if stem.endswith("_depersonalized"):
        stem = stem[: -len("_depersonalized")]

    # Remove query type prefix if present
    for prefix in ("analyze_excel_model_", "analyze_model_inputs_for_target_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break

    return stem


def _scan_models(models_dir: Path) -> dict[str, Path]:
    """Scan models_dir for .xlsx files → dict[hash] = model_path."""
    models: dict[str, Path] = {}
    if not models_dir.exists():
        print(f"WARNING: models directory does not exist: {models_dir}", file=sys.stderr)
        return models

    for f in sorted(models_dir.glob("*.xlsx")):
        h = _extract_hash(f.name)
        models[h] = f
        print(f"  Found model: {f.name} (hash={h!r})")
    return models


def _scan_queries(queries_dir: Path) -> dict[str, dict[str, Path]]:
    """Scan queries_dir for CSV files → dict[hash] = {'ema': path, 'ift': path}.

    EMA queries: analyze_excel_model_{hash}_depersonalized.csv
    IFT queries: analyze_model_inputs_for_target_{hash}_depersonalized.csv
    """
    queries: dict[str, dict[str, Path]] = {}

    if not queries_dir.exists():
        print(f"WARNING: queries directory does not exist: {queries_dir}", file=sys.stderr)
        return queries

    for f in sorted(queries_dir.glob("*.csv")):
        stem = f.stem  # e.g. analyze_excel_model_2poj431skazt_depersonalized
        h = _extract_hash(stem)

        if h not in queries:
            queries[h] = {}

        if "analyze_excel_model" in stem:
            queries[h]["ema"] = f
        elif "analyze_model_inputs_for_target" in stem:
            queries[h]["ift"] = f

    return queries


def _build_tasks(
    models: dict[str, Path],
    queries: dict[str, dict[str, Path]],
) -> list[dict[str, Any]]:
    """Build list of task dicts: {hash, model_path, ema_csv, ift_csv, ema_exists, ift_exists}."""
    tasks: list[dict[str, Any]] = []
    for h, model_path in sorted(models.items()):
        task: dict[str, Any] = {
            "hash": h,
            "model_path": model_path,
            "ema_csv": queries.get(h, {}).get("ema"),
            "ift_csv": queries.get(h, {}).get("ift"),
            "ema_exists": h in queries and "ema" in queries[h],
            "ift_exists": h in queries and "ift" in queries[h],
        }
        tasks.append(task)
    return tasks


# ================================================================
# Execution
# ================================================================

def _build_cmd(model_path: str, queries_path: str, suffix: str, output_dir: str, query_type: str) -> list[str]:
    """Build the subprocess command."""
    # Добавляем префикс к суффиксу для различения EMA и IFT файлов
    type_prefix = "ema" if query_type == "ema" else "ift"
    full_suffix = f"{type_prefix}_{suffix}"
    return [
        sys.executable, str(NEW_GRAPH_TEST),
        "--model", model_path,
        "--queries", queries_path,
        "--suffix", full_suffix,
        "--output-dir", output_dir,
        # "--batch" устарел: new_graph_test.py теперь всегда последовательный
        # с одним переиспользуемым AgentGraph на весь прогон.
    ]


async def _run_subprocess(
    log: LogWriter,
    cmd: list[str],
    description: str,
) -> subprocess.CompletedProcess[Any]:
    """Run a subprocess with **streaming** output — lines appear in real-time.

    Uses ``PYTHONUNBUFFERED=1`` so Python's stdout is line-buffered (not block).
    Reads via ``Popen`` + ``asyncio`` to interleave output on the fly.
    """
    _log_entry(log, "CMD", f"{description}: {' '.join(cmd)}")

    # Отключаем буферизацию Python в подпроцессе — тогда print/printf пишут сразу
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=env,
        limit=10**8,  # 100 MB — предотвращает LimitOverrunError на длинных строках лога
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def _stream(pipe: asyncio.StreamReader, accumulator: list[str]) -> None:
        """Read lines from a pipe and write them to log immediately."""
        while True:
            line = await pipe.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            accumulator.append(text)
            log.write(text)  # ← сразу в файл + консоль

    # Параллельно читаем stdout и stderr
    await asyncio.gather(
        _stream(proc.stdout, stdout_lines),
        _stream(proc.stderr, stderr_lines),
    )

    await proc.wait()
    result = subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )

    if result.returncode == 0:
        _log_entry(log, "OK", f"{description} completed successfully (exit 0)")
    else:
        _log_entry(log, "FAIL", f"{description} failed (exit {result.returncode})")

    return result


# ================================================================
# Summary
# ================================================================

def _build_summary_csv(
    output_dir: Path,
    run_name: str,
    task_results: list[dict[str, Any]],
) -> None:
    """Write summary.csv to experiments/{run_name}/."""
    run_dir = output_dir / run_name
    rows: list[dict[str, Any]] = []
    for tr in task_results:
        h = tr["hash"]

        # Find result files in the run directory
        ema_result_file = ""
        ift_result_file = ""
        ema_count = 0
        ift_count = 0

        for csv_file in run_dir.glob(f"ema_{h}_*_unified_results.csv"):
            ema_count = len(pd.read_csv(csv_file))
            ema_result_file = csv_file.name
            break
        for csv_file in run_dir.glob(f"ift_{h}_*_unified_results.csv"):
            ift_count = len(pd.read_csv(csv_file))
            ift_result_file = csv_file.name
            break

        row: dict[str, Any] = {
            "hash": h,
            "model_file": tr["model_path"].name,
            "ema_count": ema_count if ema_result_file else None,
            "ift_count": ift_count if ift_result_file else None,
            "ema_result_file": ema_result_file or None,
            "ift_result_file": ift_result_file or None,
            "total_duration_sec": tr.get("total_duration_sec"),
            "status": tr.get("status"),
        }
        rows.append(row)

    if not rows:
        print("No results to summarize.")
        return

    df = pd.DataFrame(rows)
    df = df.sort_values(by="hash")
    summary_path = output_dir / run_name / "summary.csv"
    df.to_csv(summary_path, index=False, encoding="utf8")
    print(f"\nSummary saved to {summary_path} ({len(df)} rows)")


def _resolve_run_name(run_dir: Path, force: bool) -> str:
    """Просит пользователя ввести новое имя прогона, если папка уже существует.

    Если запущен в режиме без TTY (pipe, CI) и папка существует — выход с ошибкой,
    если только не передан ``force=True``.
    """
    if run_dir.exists():
        if force:
            # Перезаписываем существующую папку
            return run_dir.name

        # Если нет TTY (запущено через pipe / в CI) — сразу ошибка
        if not sys.stdout.isatty():
            print(
                f"\nОШИБКА: Папка прогона уже существует: {run_dir}",
                file=sys.stderr,
            )
            print(
                "Используйте --force для перезаписи или укажите другое имя через --run-name.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Собираем статистику существующей папки
        files = list(run_dir.iterdir())
        file_count = len(files)
        mtime = datetime.fromtimestamp(run_dir.stat().st_mtime)
        time_str = mtime.strftime("%Y-%m-%d %H:%M:%S")

        print(
            f"\n⚠ ВНИМАНИЕ: Папка прогона уже существует: {run_dir}",
        )
        print(f"   Файлов: {file_count}, последняя модификация: {time_str}")
        print("Пожалуйста, введите новое имя прогона (или 'q' для выхода):")

        while True:
            try:
                new_name = input("> ").strip()
            except EOFError:
                print("\nВыход.")
                sys.exit(0)

            if not new_name:
                print("  Имя не может быть пустым. Попробуйте снова.")
                continue
            if new_name.lower() == "q":
                print("Выход.")
                sys.exit(0)

            new_dir = run_dir.parent / new_name
            if not new_dir.exists():
                return new_name

            print(f"  ⚠ Папка '{new_name}' уже существует. Введите другое имя.")

    # Папка не существует — используем заданное имя
    return run_dir.name


# ================================================================
# Main orchestrator
# ================================================================

async def _run_task(
    task: dict[str, Any],
    log: LogWriter,
    run_name: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Execute a single task: EMA + IFT for one model."""
    h = task["hash"]
    model_path = task["model_path"]
    run_dir = output_dir / run_name
    result: dict[str, Any] = {
        "hash": h,
        "model_path": model_path,
        "total_duration_sec": 0.0,
        "status": "pending",
    }

    _log_section(log, f"[{h}] Model: {model_path.name}")
    _log_entry(log, "TASK", f"Starting task for hash={h!r}, model={model_path.name}")

    start_time = time.time()

    # --- EMA ---
    if task["ema_exists"]:
        ema_csv = task["ema_csv"]  # type: ignore[assignment]
        _log_entry(log, "EMA", f"Running EMA queries: {ema_csv.name}")
        cmd = _build_cmd(str(model_path), str(ema_csv), h, str(run_dir), "ema")

        t0 = time.time()
        ema_result = await _run_subprocess(log, cmd, description=f"EMA ({h})")
        result["ema_duration"] = round(time.time() - t0, 2)

        if ema_result.returncode == 0:
            result["ema_count"] = 0
            result["ema_status"] = "ok"
            # Find the file that was just written
            for csv_file in run_dir.glob(f"ema_{h}_*_unified_results.csv"):
                df_temp = pd.read_csv(csv_file)
                result["ema_count"] = len(df_temp)
                result["ema_result_file"] = csv_file.name
                break
        else:
            result["ema_count"] = 0
            result["ema_status"] = "failed"
    else:
        _log_entry(log, "WARN", f"No EMA queries for hash={h!r}, skipping")
        result["ema_count"] = None
        result["ema_status"] = "skipped"

    # --- IFT ---
    if task["ift_exists"]:
        ift_csv = task["ift_csv"]  # type: ignore[assignment]
        _log_entry(log, "IFT", f"Running IFT queries: {ift_csv.name}")
        cmd = _build_cmd(str(model_path), str(ift_csv), h, str(run_dir), "ift")

        t0 = time.time()
        ift_result = await _run_subprocess(log, cmd, description=f"IFT ({h})")
        result["ift_duration"] = round(time.time() - t0, 2)

        if ift_result.returncode == 0:
            result["ift_count"] = 0
            result["ift_status"] = "ok"
            for csv_file in run_dir.glob(f"ift_{h}_*_unified_results.csv"):
                df_temp = pd.read_csv(csv_file)
                result["ift_count"] = len(df_temp)
                result["ift_result_file"] = csv_file.name
                break
        else:
            result["ift_count"] = 0
            result["ift_status"] = "failed"
    else:
        _log_entry(log, "WARN", f"No IFT queries for hash={h!r}, skipping")
        result["ift_count"] = None
        result["ift_status"] = "skipped"

    # --- Final status ---
    failed = any(
        k.endswith("_status") and result[k] == "failed"
        for k in result
    )
    result["total_duration_sec"] = round(time.time() - start_time, 2)
    result["status"] = "failed" if failed else "ok"

    _log_entry(log, "DONE", f"Hash {h!r}: status={result['status']}, "
               f"ema={result.get('ema_count')}, ift={result.get('ift_count')}")

    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Experiment orchestrator for running tests across multiple Excel models.",
    )
    parser.add_argument(
        "--run-name",
        required=True,
        help="Name of this experiment run (creates experiments/{run_name}/)",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=DEFAULT_MODELS_DIR,
        help=f"Directory with .xlsx model files (default: {DEFAULT_MODELS_DIR})",
    )
    parser.add_argument(
        "--queries-dir",
        type=Path,
        default=DEFAULT_QUERIES_DIR,
        help=f"Directory with CSV query files (default: {DEFAULT_QUERIES_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Base output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be done, do not execute",
    )
    parser.add_argument(
        "--max-models",
        type=int,
        default=None,
        help="Limit the number of models to process (default: all)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписать существующую папку прогона без запроса",
    )
    args = parser.parse_args()

    # --- Resolve run name (check for existing directory) ---
    run_dir = args.output_dir / args.run_name
    effective_run_name = _resolve_run_name(run_dir, force=args.force)

    # Setup log
    run_dir = args.output_dir / effective_run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "orchestrator.log"
    log = LogWriter(log_path)

    try:
        _log_section(log, f"Experiment Run: {effective_run_name}")
        if effective_run_name != args.run_name:
            _log_entry(log, "INFO", f"Original run name '{args.run_name}' renamed to '{effective_run_name}'")
        _log_entry(log, "CONFIG", f"models_dir={args.models_dir}")
        _log_entry(log, "CONFIG", f"queries_dir={args.queries_dir}")
        _log_entry(log, "CONFIG", f"output_dir={args.output_dir}")
        _log_entry(log, "CONFIG", f"dry_run={args.dry_run}")

        # --- Scan ---
        _log_section(log, "Scanning models and queries")

        models = _scan_models(args.models_dir)
        queries = _scan_queries(args.queries_dir)
        tasks = _build_tasks(models, queries)

        if args.max_models:
            tasks = tasks[: args.max_models]
            _log_entry(log, "INFO", f"Limited to {args.max_models} models")

        if not tasks:
            _log_entry(log, "WARN", "No tasks found. Exiting.")
            return

        _log_entry(log, "INFO", f"Total tasks: {len(tasks)}")
        for t in tasks:
            _log_entry(
                log,
                "TASK",
                f"  {t['hash']}: model={t['model_path'].name}, "
                f"ema={'YES' if t['ema_exists'] else 'NO'}, "
                f"ift={'YES' if t['ift_exists'] else 'NO'}",
            )

        # --- Dry run ---
        if args.dry_run:
            _log_section(log, "DRY RUN — no execution")
            _log_entry(log, "INFO", "Would execute the following tasks:")
            for t in tasks:
                lines: list[str] = []
                if t["ema_exists"]:
                    lines.append(f"  EMA: {t['ema_csv']}")  # type: ignore[union-attr]
                if t["ift_exists"]:
                    lines.append(f"  IFT: {t['ift_csv']}")  # type: ignore[union-attr]
                _log_entry(log, "TASK", f"[{t['hash']}] {' '.join(lines)}")
            _log_entry(log, "DONE", "Dry run complete.")
            return

        # --- Execute ---
        _log_section(log, "Execution")

        task_results: list[dict[str, Any]] = []
        for i, task in enumerate(tasks, 1):
            _log_entry(log, "PROGRESS", f"Task {i}/{len(tasks)}: {task['hash']}")
            tr = asyncio_run(_run_task(task, log, effective_run_name, args.output_dir))
            task_results.append(tr)

        # --- Summary ---
        _log_section(log, "Summary")
        _build_summary_csv(args.output_dir, effective_run_name, task_results)

        _log_entry(log, "DONE", f"All tasks complete. Results in {run_dir}")
    finally:
        log.close()


def asyncio_run(coro: Any) -> Any:
    """Run an async function synchronously."""
    return asyncio.run(coro)


if __name__ == "__main__":
    main()
