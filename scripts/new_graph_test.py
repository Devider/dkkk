"""
Test runner for AgentGraph subagent.

Loads prompts from an Excel or CSV file, executes each through AgentGraph,
compares actual vs expected inputs/outputs, and writes a unified CSV report.

Usage:
    python scripts/new_graph_test.py --queries tests/data/Methanex_tool_test_queries.xlsx --n 20 --start 0
    python scripts/new_graph_test.py --queries tests/data/prompts.csv --n 100 --start 0
    python scripts/new_graph_test.py --n 20 --sleep 600  # wait 10 min before running

    Arguments:
        --queries    Path to Excel (.xlsx) or CSV (.csv) file with prompts
        --model      Path to model catalog (default: models/model.xlsx)
        --start      Row index to start from (default: 0)
        --n          Number of prompts (default: 20; per sheet for Excel)
        --batch      Max concurrent test cases (default: 5)
        --suffix     Suffix for output CSV filename (default: timestamp)
        --output-dir Directory to save result CSVs (default: tests/test_results/)
        --sleep      Delay execution by N seconds; prints countdown every 30s (default: 0)

Excel sheets: analyze_excel_model | analyze_model_inputs_for_target
    ID                  — test case identifier
    Запрос (prompt)     — prompt text
    expected_call (JSON) — expected results

CSV columns:
    ID,prompt,expected_call

Output:
    {output_dir}/{suffix}{timestamp}_unified_results.csv
"""
import argparse
import asyncio
import json
import math
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd
from langchain_core.messages.human import HumanMessage

from aigw_service.api.v1.main_graph import AgentGraph
from aigw_service.api.v1.schemas.llm_outputs import InputItem
from aigw_service.config import get_store as _get_store
from aigw_service.context import APP_CTX

# Константа для ID пользователя в тесте
TEST_USER_ID = "ak_test"

# Максимальное количество сценариев для одного тестового кейса EMA.
# Если декартово произведение сетки (start→end по шагу) превышает этот лимит,
# кейс пропускается с записью в comment причины пропуска.
MAX_TEST_SCENARIOS = 1000

# Максимальное количество входных параметров для одного тестового кейса IFT.
# Превышение приводит к пропуска кейса без вызова агента.
MAX_IFT_INPUTS = 5


def _count_combinations(
    ranges: list[list[float]],
    steps: list[float],
    input_names: list[str] | None = None,
) -> tuple[int, dict[str, int]]:
    """Вычисляет количество сценариев как декартово произведение сетки.

    Для каждого параметра считает ceil((end - start) / step) + 1 значений,
    затем перемножает все количества.

    Args:
        ranges: Список пар [[start, end], ...] для каждого параметра.
        steps: Список шагов [step1, step2, ...] для каждого параметра.
        input_names: Опционально — имена параметров для детализации (по умолчанию None).

    Returns:
        Кортеж (total_combinations, per_input_counts).
        per_input_counts — dict {input_name: n_values} если input_names передан,
        иначе dict с индексами.
    """
    per_input: dict[str, int] = {}
    for i, (rng, step) in enumerate(zip(ranges, steps, strict=True)):
        start, end = rng
        step_val = step if step and step > 0 else 0.5
        n_values = math.ceil((end - start) / step_val) + 1
        key = input_names[i] if input_names and i < len(input_names) else f"param_{i}"
        per_input[key] = n_values

    total = math.prod(per_input.values())
    return total, per_input


_TYPE_RE = re.compile(r"analyze_(excel_model|model_inputs_for_target)")


def classify_file_by_name(filename: str) -> str:
    m = _TYPE_RE.search(filename)
    if not m:
        raise ValueError(f"Unknown file type: {filename}")
    # "excel_model" → analyze_excel_model, "model_inputs_for_target" → analyze_model_inputs_for_target
    return f"analyze_{m.group(1)}"

def _read_prompts_xlsx(path: str, n: int, start: int) -> list[dict]:
    """Collect up to n prompts per sheet from an Excel file.

    Args:
        path: Path to the Excel file with prompts.
        n: Maximum number of prompts to read per sheet.
        start: Row index to skip (start reading from this row).

    Returns:
        List of dicts with keys: id, prompt, expected.
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[dict] = []
    for sheet_name in ["analyze_excel_model", "analyze_model_inputs_for_target"]:
        ws = wb[sheet_name]
        headers = [c.value for c in next(ws.iter_rows(max_row=1))]
        col_map = {h: i for i, h in enumerate(headers)}
        taken = 0
        row_idx = 0
        if start > 0:
            print(f"We start from the row # {start}")
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[col_map.get("ID", 0)]:
                continue

            if row_idx < start:
                print(f"Skip {row[col_map['ID']]!s} because starting position is: {start}")
                row_idx += 1
                continue
            # Проверяем количество комбинаций для analyze_excel_model
            expected_data = json.loads(row[col_map["expected_call (JSON)"]])
            skip_reason = None
            if sheet_name == "analyze_excel_model":
                ranges = expected_data.get("ranges")
                steps = expected_data.get("steps")
                input_names = expected_data.get("input_names")
                if ranges and steps:
                    total_combos, per_input = _count_combinations(ranges, steps, input_names)
                    if total_combos > MAX_TEST_SCENARIOS:
                        detail = "; ".join(f"{name}: {cnt}" for name, cnt in per_input.items())
                        skip_reason = (
                            f"Пропускаем: {total_combos:,} сценариев (макс {MAX_TEST_SCENARIOS:,}). "
                            f"Диапазоны: {detail}"
                        )
            elif sheet_name == "analyze_model_inputs_for_target":
                n_inputs = len(expected_data.get("input_names", []))
                if n_inputs > MAX_IFT_INPUTS:
                    skip_reason = (
                        f"Пропускаем: {n_inputs} input-параметров (макс {MAX_IFT_INPUTS}). "
                        f"При N > 4 экспоненциальный рост декартова произведения."
                    )

            out.append(
                {
                    "id": str(row[col_map["ID"]]),
                    "prompt": str(row[col_map["Запрос (prompt)"]]),
                    "expected_class": sheet_name,
                    "expected": expected_data,
                    "skipped_reason": skip_reason,
                }
            )
            taken += 1
            if taken >= n:
                break
    wb.close()
    return out


def _read_prompts_csv(path: str, n: int, start: int) -> list[dict]:
    """Collect up to n prompts from a CSV file.

    CSV columns: ID, prompt, expected_call

    Args:
        path: Path to the CSV file with prompts.
        n: Maximum number of prompts to read.
        start: Row index to skip (start reading from this row).

    Returns:
        List of dicts with keys: id, prompt, expected, expected_class.
    """
    expected_class = classify_file_by_name(Path(path).name)
    df = pd.read_csv(path)
    # Normalize column names (lowercase, strip whitespace, remove parentheses)
    def _normalize_col(c: str) -> str:
        return c.strip().lower().replace("(", "").replace(")", "").replace(" ", "_")

    df.columns = [_normalize_col(c) for c in df.columns]

    def _find_col(df_cols: list[str], *candidates: str) -> str | None:
        """Find the first matching column name in a list of column names."""
        for candidate in candidates:
            for col in df_cols:
                if candidate in col:
                    return col
        return None

    id_col = _find_col(df.columns, "id")
    prompt_col = _find_col(df.columns, "prompt", "запрос")
    expected_col = _find_col(df.columns, "expected_call", "expected")

    if not id_col:
        raise ValueError(f"'ID' column not found. Available columns: {list(df.columns)}")
    if not prompt_col:
        raise ValueError(f"'prompt' column not found. Available columns: {list(df.columns)}")
    if not expected_col:
        raise ValueError(f"'expected_call' column not found. Available columns: {list(df.columns)}")

    out: list[dict] = []
    taken = 0
    row_idx = 0
    if start > 0:
        print(f"We start from the row # {start}")

    for _, row in df.iterrows():
        row_id = row.get(id_col)
        if pd.isna(row_id) or not str(row_id).strip():
            continue

        if row_idx < start:
            print(f"Skip {row_id} because starting position is: {start}")
            row_idx += 1
            continue

        try:
            expected_raw = row[expected_col]
            if pd.isna(expected_raw):
                expected = {}
            else:
                expected = json.loads(str(expected_raw))
        except json.JSONDecodeError as e:
            print(f"Warning: failed to parse expected_call for row {row_id}: {e}")
            expected = {}

        # Проверяем количество комбинаций для analyze_excel_model
        skip_reason = None
        if expected_class == "analyze_excel_model":
            ranges = expected.get("ranges")
            steps = expected.get("steps")
            input_names = expected.get("input_names")
            if ranges and steps:
                total_combos, per_input = _count_combinations(ranges, steps, input_names)
                if total_combos > MAX_TEST_SCENARIOS:
                    detail = "; ".join(f"{name}: {cnt}" for name, cnt in per_input.items())
                    skip_reason = (
                        f"Пропускаем: {total_combos:,} сценариев (макс {MAX_TEST_SCENARIOS:,}). "
                        f"Диапазоны: {detail}"
                    )
        elif expected_class == "analyze_model_inputs_for_target":
            n_inputs = len(expected.get("input_names", []))
            if n_inputs > MAX_IFT_INPUTS:
                skip_reason = (
                    f"Пропускаем: {n_inputs} input-параметров (макс {MAX_IFT_INPUTS}). "
                    f"При N > 4 экспоненциальный рост декартова произведения."
                )

        out.append(
            {
                "id": str(row[id_col]),
                "prompt": str(row[prompt_col]),
                "expected": expected,
                "expected_class": expected_class,
                "skipped_reason": skip_reason,
            }
        )
        taken += 1
        if taken >= n:
            break

    return out


def _read_prompts(path: str, n: int, start: int) -> list[dict]:
    """Read prompts from an Excel (.xlsx) or CSV (.csv) file.

    Args:
        path: Path to the input file.
        n: Maximum number of prompts to read.
        start: Row index to skip.

    Returns:
        List of dicts with keys: id, prompt, expected.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "csv":
        return _read_prompts_csv(path, n, start)
    else:
        return _read_prompts_xlsx(path, n, start)


# ================================================================
# Unified schema constants
# ================================================================

COMMON_COLS = [
    "id",
    "prompt",
    "expected_class",
    "actual_class",
    "duration_sec",
    "comment",
    "final_state",
]

EMA_COLS = [
    "expected_inputs",
    "actual_inputs",
    "expected_outputs",
    "actual_outputs",
    "expected_year",
    "actual_year",
    "expected_ranges",
    "actual_ranges",
    "expected_steps",
    "actual_steps",
]

IFT_COLS = [
    "expected_inputs",
    "actual_inputs",
    "expected_output",
    "actual_output",
    "expected_target",
    "actual_target",
    "expected_year",
    "actual_year",
]

ALL_COLS = COMMON_COLS + EMA_COLS + IFT_COLS


def _build_ema_result(
    case_id: str,
    prompt: str,
    classification_result: str | None,
    expected_class: str,
    query: dict,
    result: dict,
    duration: float,
    error_msg: str,
) -> dict:
    """Build unified result for EMA (analyze_excel_model) scenario."""
    actual_inputs = result.get("ema_resolved_inputs") or []
    analysis = result.get("ema_analizer_results")
    actual_year = analysis.year if analysis else None
    actual_outputs = (
        [o.equivalent_output_name for o in analysis.mentioned_outputs]
        if analysis and analysis.mentioned_outputs
        else []
    )

    actual_input_names: list[str] = []
    actual_ranges: list[list[float]] = []
    actual_steps: list[float | None] = []
    for inp in actual_inputs:
        if isinstance(inp, InputItem) and inp.range_config:
            actual_input_names.append(inp.equivalent_input_name)
            actual_ranges.append([inp.range_config.start_value, inp.range_config.end_value])
            actual_steps.append(inp.range_config.step)

    expected = query.get("expected", {})
    return {
        "id": case_id,
        "prompt": prompt,
        "expected_class": expected_class,
        "actual_class": classification_result,
        "duration_sec": duration,
        "comment": error_msg,
        # EMA
        "expected_inputs": expected.get("input_names"),
        "actual_inputs": actual_input_names,
        "expected_outputs": expected.get("output_names"),
        "actual_outputs": actual_outputs,
        "expected_year": expected.get("year"),
        "actual_year": actual_year,
        "expected_ranges": expected.get("ranges"),
        "actual_ranges": actual_ranges,
        "expected_steps": expected.get("steps"),
        "actual_steps": actual_steps,
        # IFT
        "expected_output": None,
        "actual_output": None,
        "expected_target": None,
        "actual_target": None,
        # final_state
        "final_state": json.dumps(result, default=str, ensure_ascii=False),
    }


def _build_ift_result(
    case_id: str,
    prompt: str,
    classification_result: str | None,
    expected_class: str,
    query: dict,
    result: dict,
    duration: float,
    error_msg: str,
) -> dict:
    """Build unified result for IFT (analyze_model_inputs_for_target) scenario."""
    analysis = result.get("ift_analyzer_results")
    actual_inputs = result.get("ift_resolved_inputs") or []
    actual_output = analysis.output_name if analysis else None
    actual_target = analysis.target_value if analysis else None
    actual_year = analysis.output_year if analysis else None

    expected = query.get("expected", {})
    return {
        "id": case_id,
        "prompt": prompt,
        "expected_class": expected_class,
        "actual_class": classification_result,
        "duration_sec": duration,
        "comment": error_msg,
        # IFT
        "expected_inputs": expected.get("input_names"),
        "actual_inputs": actual_inputs,
        "expected_output": expected.get("output_name"),
        "actual_output": actual_output,
        "expected_target": expected.get("target_value"),
        "actual_target": actual_target,
        "expected_year": expected.get("output_year"),
        "actual_year": actual_year,
        # EMA
        "expected_outputs": None,
        "actual_outputs": None,
        "expected_ranges": None,
        "actual_ranges": None,
        "expected_steps": None,
        "actual_steps": None,
        # final_state
        "final_state": json.dumps(result, default=str, ensure_ascii=False),
    }


async def run_test_case(agent: AgentGraph, q: dict[str, Any]) -> dict:
    """Execute a single test case against AgentGraph and collect results.

    Invokes the agent with the given prompt and a direct file_path.

    Args:
        agent: AgentGraph instance.
        q: Test case dict with keys: id, prompt, expected.

    Returns:
        Unified result dict with scenario_type, expected_class, actual_class,
        and scenario-specific fields.
    """
    # Пропускаем кейсы с слишком большим количеством комбинаций
    skip_reason = q.get("skipped_reason")
    if skip_reason:
        print(f"[SKIP] {q['id']}: {skip_reason}")
        return {
            "id": q["id"],
            "prompt": q["prompt"],
            "expected_class": q["expected_class"],
            "actual_class": None,
            "duration_sec": 0.0,
            "comment": skip_reason,
            "final_state": None,
            # Fill all scenario-specific cols with None
            "expected_inputs": None,
            "actual_inputs": None,
            "expected_outputs": None,
            "actual_outputs": None,
            "expected_year": None,
            "actual_year": None,
            "expected_ranges": None,
            "actual_ranges": None,
            "expected_steps": None,
            "actual_steps": None,
            "expected_output": None,
            "actual_output": None,
            "expected_target": None,
            "actual_target": None,
        }

    print(f"Prompt: {q['id']}")
    prompt = q["prompt"]
    print(f"Prompt: {prompt}")

    expected_class = q["expected_class"]

    start_time = time.time()
    error_msg = ""
    final_state = None
    try:
        result = await agent.graph.ainvoke(
            {
                "messages": [HumanMessage(content=prompt)],
                "user_id": "ak_test",
            }
        )
        final_state = result
        classification_result = result.get("classification_result")
        match classification_result:
            case "analyze_excel_model":
                stat = _build_ema_result(
                    q["id"],
                    prompt,
                    classification_result,
                    expected_class,
                    q,
                    result,
                    time.time() - start_time,
                    error_msg,
                )
            case "analyze_model_inputs_for_target":
                stat = _build_ift_result(
                    q["id"],
                    prompt,
                    classification_result,
                    expected_class,
                    q,
                    result,
                    time.time() - start_time,
                    error_msg,
                )
            case _:
                raise ValueError(f"Unknown classification: {classification_result}")
    except Exception as e:  # noqa: BLE001
        error_msg = str(e)
        print(error_msg)
        stat = {}
        # Return minimal dict with error info
        stat = {
            "id": q["id"],
            "prompt": prompt,
            "expected_class": expected_class,
            "actual_class": "unknown",
            "duration_sec": round(time.time() - start_time, 2),
            "comment": error_msg,
        }
        # Fill all cols with None
        for col in ALL_COLS:
            if col not in stat:
                stat[col] = None
    finally:
        stat["final_state"] = final_state

    return stat


async def run_batch(
    queries: list[dict],
) -> list[dict]:
    """Run test cases sequentially, reusing a single ``AgentGraph`` instance.

    Creates one ``AgentGraph`` once and reuses it for all queries.
    This avoids the overhead of compiling the graph and sub-agents
    for every single test case.
    """
    agent = AgentGraph()
    results: list[dict] = []
    for i, q in enumerate(queries, 1):
        res = await run_test_case(agent, q)
        results.append(res)
        print(f"[ {i}/{len(queries)}] за {res.get('duration_sec', '?')}s")
    return results


async def _prepare_store(model_path: str) -> None:
    """Скопировать файл модели в /tmp и записать имя в InMemoryStore.

    Аналог upload-эндпоинта (router.py:66–85):
        namespace = ("memories", user_id)
        key = user_id
        await store.aput(namespace, key, {"filename": filename})

    Args:
        model_path: Путь к файлу модели (например, ``models/model.xlsx``).
    """
    store = APP_CTX.agent_memory.store
    if store is None:
        raise RuntimeError("Agent memory store is not initialized. "
                           "Call _get_store() and assign to APP_CTX.agent_memory.store first.")

    # 1. Копируем файл в /tmp с именем: user_file_{user_id}_{original_name}.xlsx
    temp_dir = Path(tempfile.gettempdir())
    file_ext = Path(model_path).suffix  # .xlsx
    file_stem = Path(model_path).stem   # название без расширения
    filename = f"{file_stem}{file_ext}"
    dest = temp_dir / filename

    shutil.copy2(model_path, dest)
    print(f"Скопировано {model_path} -> {dest}")

    # 2. Записываем имя файла в store (namespace, key) = ("memories", user_id)
    namespace = ("memories", TEST_USER_ID)
    key = TEST_USER_ID
    await store.aput(namespace, key, {"filename": filename})
    stored = await store.aget(namespace, key)
    print(f"Запись в store: namespace={namespace}, key={key}, value={stored}")


async def main():
    """CLI entry point: parse args, run all test cases, save unified CSV report."""
    parser = argparse.ArgumentParser(description="AgentGraph subagent test runner")
    parser.add_argument("--queries", default="tests/data/Methanex_tool_test_queries.xlsx")
    parser.add_argument("--model", default="models/model.xlsx")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--suffix", default=None,
                        help="Suffix for the output CSV filename (default: timestamp)")
    parser.add_argument("--output-dir", default="tests/test_results",
                        help="Directory to save result CSVs (default: tests/test_results)")
    parser.add_argument("--sleep", type=int, default=0,
                        help="Delay execution by N seconds (countdown every 30s)")
    args = parser.parse_args()

    if args.sleep > 0:
        print(f"⏳ Waiting {args.sleep}s before starting... ", end="", flush=True)
        for i in range(args.sleep):
            remaining = args.sleep - i
            print(f"\r⏳ Waiting {remaining}s before starting...", end="", flush=True)
            await asyncio.sleep(1)
        print()
        print("▶ Starting tests now!", flush=True)

    queries = _read_prompts(args.queries, args.n, args.start)

    # 1. Инициализируем store вручную — on_startup() не вызывается при запуске скрипта
    if APP_CTX.agent_memory.store is None:
        APP_CTX.agent_memory.store = _get_store()
        print("Initialized InMemoryStore for agent memory.")

    # 2. Загружаем файл модели в store (аналог /upload эндпоинта)
    print(f"Подготовка файла модели: {args.model}")
    await _prepare_store(args.model)

    try:
        results_list = await run_batch(queries)
    finally:
        _save_to_csv(results_list, args.suffix, args.output_dir)


def _save_to_csv(results: list[dict], suffix: str | None = None, output_dir: str = "tests/test_results") -> None:
    """Write results to separate CSV files per scenario type (EMA / IFT)."""
    if not results:
        return

    if suffix is None:
        suffix = ""
    else:
        suffix = suffix + "_"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")  # noqa: DTZ005
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Разделяем результаты по типу сценария
    ema_results: list[dict] = []
    ift_results: list[dict] = []
    for r in results:
        cls = r.get("expected_class")
        if cls == "analyze_excel_model":
            ema_results.append(r)
        elif cls == "analyze_model_inputs_for_target":
            ift_results.append(r)
        # else — неопознанный тип, игнорируем

    # Функция для сбора статистики
    def _stats(rows: list[dict]) -> tuple[int, int, int]:
        total = len(rows)
        if total == 0:
            return 0, 0, 0
        skipped = sum(1 for r in rows if r.get("comment"))
        return total, total - skipped, skipped

    # EMA файл
    if ema_results:
        df_ema = pd.DataFrame(ema_results, columns=COMMON_COLS + EMA_COLS).sort_values(by="id")
        total, run, skipped = _stats(ema_results)
        csv_filename = out_dir / f"{suffix}{timestamp}_results.csv"
        df_ema.to_csv(csv_filename, index=False, encoding="utf8")
        print(f"\nEMA (analyze_excel_model): {csv_filename} ({total} rows)")
        if skipped:
            print(f"  ✅ Выполнено: {run} ({run/total*100:.0f}%)")
            print(f"  ⏭️  Пропущено: {skipped} ({skipped/total*100:.0f}%)")

    # IFT файл
    if ift_results:
        df_ift = pd.DataFrame(ift_results, columns=COMMON_COLS + IFT_COLS).sort_values(by="id")
        total, run, skipped = _stats(ift_results)
        csv_filename = out_dir / f"{suffix}{timestamp}_results.csv"
        df_ift.to_csv(csv_filename, index=False, encoding="utf8")
        print(f"IFT (analyze_model_inputs_for_target): {csv_filename} ({total} rows)")
        if skipped:
            print(f"  ✅ Выполнено: {run} ({run/total*100:.0f}%)")
            print(f"  ⏭️  Пропущено: {skipped} ({skipped/total*100:.0f}%)")

    # Если оба пустые
    if not ema_results and not ift_results:
        print(f"\n⚠️ No results to save (0 EMA, 0 IFT)")


if __name__ == "__main__":
    asyncio.run(main())
