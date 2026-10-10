import os
import random
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from itertools import product
from typing import Any, Optional

import numpy as np
import pandas as pd
from pandas import ExcelWriter
from pydantic import BaseModel, Field  # type: ignore

from aigw_service.api.v2.excel_handler import ExcelWorkbook
from aigw_service.api.v2.russian_stemmer import russian_stem
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

TEMP_DIR = tempfile.gettempdir()


def _humanize_error(e: Exception) -> str:
    msg = str(e)
    if "MultiCellRange" in msg:
        return (
            "Excel-файл содержит повреждённые объединённые ячейки (merged cells). "
            "Откройте файл в Excel или LibreOffice, сохраните его заново (File → Save As → .xlsx) "
            "и загрузите снова через /api/v2/upload."
        )
    return msg


# LRU cache for analyze_excel_model — LLMs often repeat identical queries
_ANALYSIS_CACHE: dict = {}
_ANALYSIS_CACHE_ORDER: list = []
_ANALYSIS_CACHE_MAXSIZE = 10
# Максимальное количество сценариев для анализа одного вызова
MAX_SCENARIOS = 100000
# Result classes


class ExcelAnalysisToolResult(BaseModel):
    status: str = Field(description="Статус анализа")
    result: str = Field(description="Результат работы функции")
    content: dict[str, Any] = Field(description="Результат анализа в формате JSON")


class ModelInputAnalysisToolResult(BaseModel):
    status: str = Field(description="Статус анализа")
    result: str = Field(description="Результат работы функции")
    content: dict[str, Any] = Field(
        description="Результат анализа входных параметров для достижения целевого значения"
    )


# Args classes
class ExcelAnalysisToolArgs(BaseModel):
    file_name: str = Field(..., description="Название Excel файла для анализа")
    input_names: list[str] = Field(
        ..., description="Список описательных запросов для идентификации ячеек на листе 'inputs'."
    )
    output_names: list[str] = Field(
        ..., description="Список описательных запросов для идентификации ячеек на листе 'outputs'."
    )
    output_years: list[int] = Field(..., description="Список лет для каждого выходного параметра")
    ranges: list[list[float]] = Field(
        ...,
        description="Диапазоны [начало, конец] для каждой ячейки, соответствующей запросу в input_names. Порядок и количество должны совпадать с input_names.",
    )
    steps: list[float] = Field(
        default=[0.5],
        description="Список шагов для генерации значений входных переменных. Если шагов меньше чем input_names, последний шаг будет использован для оставшихся переменных.",
    )
    user_id: Optional[str] = Field(default=None, description="ID пользователя")
    thread_id: Optional[str] = Field(default=None, description="ID потока")


class ModelInputAnalysisToolArgs(BaseModel):
    file_name: str = Field(..., description="Название Excel файла для анализа")
    output_name: str = Field(
        ..., description="Описательный запрос для идентификации целевой ячейки на листе 'outputs' (без года)"
    )
    output_year: int = Field(..., description="Год целевого показателя (например: 2028)")
    target_value: float = Field(
        ..., description="Целевое число без единиц измерения и суффиксов. Например: 1.85, 3650, -270"
    )
    tolerance: float = Field(default=0.1, description="Допустимое отклонение от целевого значения")
    max_scenarios: int = Field(default=1000, description="Максимальное количество сценариев для анализа")
    input_names: list[str] = Field(
        ..., description="Список описательных запросов для идентификации ячеек на листе 'inputs' (без года)."
    )
    thread_id: str = Field(default="", description="ID потока")
    user_id: str = Field(default="", description="ID пользователя")


# @tool(args_schema=ModelInputAnalysisToolArgs)
def analyze_model_inputs_for_target(
    file_name: str,
    output_name: str,
    output_year: int,
    target_value: float,
    input_names: list[str],
    tolerance: float = 0.1,
    max_scenarios: int = 1000,
    thread_id: str = "",
    user_id: str = "",
) -> ModelInputAnalysisToolResult:
    """
    Подбирает параметры модели, которые приводят к требуемому значению целевого параметра.

    Args:
        file_name (str): Имя Excel файла для анализа
        output_name (str): Описательный запрос для идентификации целевой ячейки (без года)
        output_year (int): Год целевого показателя
        target_value (float): Целевое значение для выходного параметра (без единиц и суффиксов)
        input_names (list[str]): Список входных параметров для анализа (без года)
        tolerance (float): Допустимое отклонение от целевого значения в процентах (по умолчанию 0.1%)
        max_scenarios (int): Максимальное количество сценариев для анализа (по умолчанию 1000)

    Returns:
        ModelInputAnalysisToolResult: Результат анализа с найденными сценариями
    """
    try:
        # Validate inputs
        if not input_names:
            return ModelInputAnalysisToolResult(
                status="ERROR", result="Не указаны входные параметры для анализа", content={}
            )

        # Setup Excel — try user's uploaded file first, fall back to file_name from LLM
        file_path = None
        if user_id:
            stored_name = get_store_file(user_id)
            logger.info(f"Stored name: {stored_name}")
            if stored_name:
                file_path = os.path.abspath(os.path.join(TEMP_DIR, stored_name))
                logger.info(f"File path: {file_path}")
        if not file_path or not os.path.exists(file_path):
            file_path = os.path.abspath(os.path.join(TEMP_DIR, file_name))
            logger.info(f"Constructed file path: {file_path}")
        if not os.path.exists(file_path):
            return ModelInputAnalysisToolResult(status="ERROR", result=f"Файл {file_name} не найден", content={})

        # Cache (LLMs often repeat identical target queries — the LO backend is slow)
        cache_key = (
            file_path,
            output_name,
            output_year,
            target_value,
            tuple(input_names),
            tolerance,
            max_scenarios,
            user_id,
        )
        cached = _ANALYSIS_CACHE.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for analyze_model_inputs_for_target — returning cached result")
            return cached

        start_time = time.perf_counter()
        with ExcelWorkbook(file_path) as xl:
            input_mapping = create_input_mapping(xl.get_all_data("Inputs"))
            output_mapping = create_output_mapping(xl.get_all_data("Outputs"))

            # Find and validate output cell
            try:
                output_info = find_matching_outputs(output_name, output_mapping)
                if not output_info:
                    return ModelInputAnalysisToolResult(
                        status="ERROR", result=f'Выходной параметр "{output_name}" не найден', content={}
                    )
                actual_output_name = list(output_info.keys())[0]
                target_year = output_year
                output_cell_ref = get_output_cell_ref(output_mapping, actual_output_name, target_year)
                output_cell_value = xl.get_cell("Outputs", output_cell_ref)

                logger.info(
                    f"OUTPUT MATCHING: Output '{output_name}' → Found: '{actual_output_name}' at {output_cell_ref} = {output_cell_value}"
                )
                if output_cell_value is None:
                    logger.warning(
                        "Output cell {} -> {} at {} returned None (cell exists but no formula)",
                        output_name,
                        actual_output_name,
                        output_cell_ref,
                    )

            except Exception as e:
                return ModelInputAnalysisToolResult(
                    status="ERROR", result=f"Ошибка при поиске выходного параметра: {e!s}", content={}
                )

            # Find and validate input cells (use output year as default if not found)
            input_cells = {}
            current_values = {}
            for name in input_names:
                try:
                    cell_address, original_name = find_matching_cell(name, input_mapping, default_year=target_year)
                    cur_val = xl.get_cell("Inputs", cell_address)
                    input_cells[name] = {
                        "cell_ref": cell_address,
                        "original_name": original_name,
                        "current_value": float(cur_val) if cur_val is not None else None,
                    }
                    if cur_val is not None:
                        current_values[name] = float(cur_val)

                    logger.info(
                        f"INPUT MATCHING: Input '{name}' → Found: '{original_name}' at {cell_address} = {cur_val}"
                    )

                except ValueError as e:
                    return ModelInputAnalysisToolResult(
                        status="ERROR",
                        result=f'Не удалось найти ячейку для параметра "{name}": {e!s}',
                        content={},
                    )

            # Generate scenarios
            scenarios = generate_scenarios(
                input_cells=input_cells, current_values=current_values, max_scenarios=max_scenarios
            )

            logger.info(
                "IFT calculation started: file={}, output={}, target={}, scenarios={}",
                os.path.basename(file_path),
                actual_output_name,
                target_value,
                len(scenarios),
            )

            # Compile a LibreOffice function for fast repeated evaluation
            fname = os.path.basename(file_path)
            input_refs = [f"'[{fname}]INPUTS'!{input_cells[n]['cell_ref']}" for n in input_names]
            output_ref = f"'[{fname}]OUTPUTS'!{output_cell_ref}"
            func = xl.get_compiled_func(input_refs, [output_ref])

            # Test scenarios (in-memory, no LibreOffice)
            results = test_scenarios(
                func=func,
                scenarios=scenarios,
                input_cells=input_cells,
                target_value=target_value,
                tolerance=tolerance,
            )

            # Optimize using regression to refine beyond the grid resolution
            optimized = optimize_with_regression(
                func=func,
                scenarios=results["all_scenarios"],
                input_names=input_names,
                target_value=target_value,
            )
            if optimized:
                logger.info(f"Optimized scenario found: {optimized}")

            # Fallback: if no matching scenarios and optimization succeeded, accept it
            if (
                not results["matching_scenarios"]
                and optimized
                and optimized.get("deviation_percent", 1e9) <= tolerance
            ):
                results["matching_scenarios"].append(
                    {
                        "input_values": optimized["input_values"],
                        "output_value": optimized["actual_output"],
                        "deviation": optimized["deviation"],
                        "deviation_percent": optimized["deviation_percent"],
                    }
                )

            # Save results to Excel
            excel_file = save_analysis_results(
                scenarios=results["all_scenarios"],
                optimized_scenario=optimized,
                input_names=input_names,
                output_name=output_name,
                target_value=target_value,
                tolerance=tolerance,
                search_config=results["search_config"],
            )

            # Prepare final results
            final_results = {
                "target_output": output_name,
                "actual_output_name": actual_output_name,
                "target_value": target_value,
                "tolerance_percent": tolerance,
                "scenarios_found": len(results["matching_scenarios"]),
                "total_scenarios_tested": len(scenarios),
                "processing_time_seconds": round(time.perf_counter() - start_time, 2),
                "search_configuration": results["search_config"],
                "matching_scenarios": results["matching_scenarios"][:10],
                "all_scenarios": results["all_scenarios"][:50],
                "input_names": input_names,
                "current_input_values": current_values,
                "optimized_scenario": optimized,
                "results_file": excel_file,
            }

            # After scenario testing and before result generation
            elapsed_total = round(time.perf_counter() - start_time, 2)
            logger.info(
                "IFT calculation completed: elapsed={}s, matching_scenarios={}, total_scenarios={}",
                elapsed_total,
                len(results["matching_scenarios"]),
                len(scenarios),
            )

            # Generate result message
            message = generate_result_message(
                matching_scenarios=results["matching_scenarios"],
                scenarios_tested=len(scenarios),
                target_value=target_value,
                tolerance=tolerance,
                search_config=results["search_config"],
                processing_time=results["processing_time"],
            )

            result = ModelInputAnalysisToolResult(
                status="OK" if results["matching_scenarios"] else "WARNING", result=message, content=final_results
            )

            # Cache successful result
            _ANALYSIS_CACHE[cache_key] = result
            _ANALYSIS_CACHE_ORDER.append(cache_key)
            if len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_MAXSIZE:
                oldest = _ANALYSIS_CACHE_ORDER.pop(0)
                _ANALYSIS_CACHE.pop(oldest, None)

            return result

    except Exception as e:
        logger.opt(exception=True).error("Ошибка при анализе: {}", str(e))
        msg = _humanize_error(e)
        return ModelInputAnalysisToolResult(status="ERROR", result=msg, content={})


def generate_scenarios(input_cells: dict, current_values: dict, max_scenarios: int) -> list:
    """Generate test scenarios for input parameters."""
    num_inputs = len(input_cells)
    steps_per_input = int(max_scenarios ** (1.0 / num_inputs))
    steps_per_input = max(4, min(steps_per_input, 10))  # Between 4 and 10 steps

    # Generate value ranges for each input
    ranges = {}
    steps = {}
    for name, info in input_cells.items():
        current = current_values[name]
        ranges[name] = [current * 0.5, current * 1.5]  # ±50% from current
        range_size = ranges[name][1] - ranges[name][0]
        steps[name] = range_size / (steps_per_input - 1)

    # Generate value sets
    value_sets = []
    for name in input_cells:
        start, end = ranges[name]
        step = steps[name]
        values = np.arange(start, end + 1e-10, step)
        value_sets.append(values.tolist())

    # Generate combinations
    scenarios = list(product(*value_sets))
    if len(scenarios) > max_scenarios:
        random.shuffle(scenarios)
        scenarios = scenarios[:max_scenarios]

    return scenarios


def _formula_scalar(val) -> float:
    """Extract a scalar float from a compiled-function return value."""
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if val is None:
        raise TypeError("cell value is None")
    return float(val)


def test_scenarios(func: Callable, scenarios: list, input_cells: dict, target_value: float, tolerance: float) -> dict:
    """Test scenarios using a compiled LibreOffice function and collect results."""
    matching_scenarios = []
    all_scenarios = []
    start_time = time.perf_counter()
    input_names = list(input_cells)

    for i, values in enumerate(scenarios):
        scenario_inputs = dict(zip(input_names, values, strict=True))

        try:
            result = func(*values)
            # func returns a list of output values (one per output ref)
            output = _formula_scalar(result[0])

            deviation = abs(output - target_value)
            deviation_percent = (deviation / target_value) * 100

            scenario = {
                "input_values": scenario_inputs,
                "output_value": output,
                "deviation": deviation,
                "deviation_percent": deviation_percent,
            }

            all_scenarios.append(scenario)
            logger.info(
                "Scenario {}: output={:.3f}, deviation={:.4f}, deviation_percent={:.2f}%, tolerance={}",
                i,
                output,
                deviation,
                deviation_percent,
                tolerance,
            )
            if deviation_percent <= tolerance:
                matching_scenarios.append(scenario)

        except (ValueError, TypeError):
            continue

    processing_time = time.perf_counter() - start_time

    return {
        "matching_scenarios": matching_scenarios,
        "all_scenarios": all_scenarios,
        "processing_time": processing_time,
        "search_config": {
            "input_ranges": {
                name: {
                    "min": min(s["input_values"][name] for s in all_scenarios),
                    "max": max(s["input_values"][name] for s in all_scenarios),
                }
                for name in input_names
            }
        },
    }


def optimize_with_regression(
    func: Callable,
    scenarios: list,
    input_names: list,
    target_value: float,
) -> dict:
    """Optimize inputs using scipy minimize with the compiled LibreOffice function."""
    try:
        from scipy.optimize import minimize

        # Prepare data — sort scenarios by deviation ascending, take best
        sorted_scenarios = sorted(scenarios, key=lambda s: s["deviation"])
        best = sorted_scenarios[0]

        # Bounds from the scenario ranges
        bounds = [
            (min(s["input_values"][name] for s in scenarios), max(s["input_values"][name] for s in scenarios))
            for name in input_names
        ]

        # Objective: minimise |actual_output - target|
        def objective(x):
            out = func(*x)
            return abs(_formula_scalar(out) - target_value)

        # Start from the best grid scenario
        x0 = np.array([best["input_values"][name] for name in input_names])
        res = minimize(objective, x0, bounds=bounds, method="L-BFGS-B")

        # Evaluate the optimised point with the real model
        actual = _formula_scalar(func(*res.x))

        input_values = {name: round(float(v), 3) for name, v in zip(input_names, res.x, strict=True)}
        deviation = abs(actual - target_value)
        deviation_percent = deviation / target_value * 100

        return {
            "input_values": input_values,
            "actual_output": round(float(actual), 3),
            "deviation": round(float(deviation), 3),
            "deviation_percent": round(float(deviation_percent), 2),
            "optimized": True,
        }
    except Exception as e:
        logger.opt(exception=True).warning("Optimization failed: {}", str(e))
        return None


def get_output_cell_ref(mapping: dict, output_name: str, year: int) -> str:
    """Return cell reference (e.g. ``"B12"``) for an output cell."""
    year_col_idx = None
    for col_idx, y in mapping["year_columns"].items():
        if y == year:
            year_col_idx = col_idx
            break

    if year_col_idx is None:
        raise ValueError(f"Year {year} not found")

    # 0‑based → 1‑based for openpyxl's get_column_letter
    from openpyxl.utils import get_column_letter

    col_letter = get_column_letter(year_col_idx + 1)

    row = None
    for _, info in mapping["output_mapping"].items():
        if info["original"] == output_name:
            row = info["row"]
            break

    if row is None:
        raise ValueError(f"Output '{output_name}' not found")

    return f"{col_letter}{row}"


def generate_result_message(
    matching_scenarios: list,
    scenarios_tested: int,
    target_value: float,
    tolerance: float,
    search_config: dict,
    processing_time: float,
) -> str:
    """Generate human-readable result message."""
    # Create range description
    range_info = []
    for name, range_data in search_config["input_ranges"].items():
        range_info.append(f"{name}: {round(range_data['min'], 2)} to {round(range_data['max'], 2)}")
    range_description = "\nДиапазоны поиска:\n" + "\n".join(range_info)

    # Create main message
    if matching_scenarios:
        # Build a summary of the best matching scenarios
        best = matching_scenarios[:3]
        details = []
        for s in best:
            inv = " | ".join(f"{k}={round(v, 4)}" for k, v in s["input_values"].items())
            details.append(f"{inv} → {s['output_value']:.2f} (отклонение {s['deviation_percent']:.2f}%)")
        detail_str = "\n".join(details)
        return (
            f"Найдено {len(matching_scenarios)} сценариев, приводящих к целевому значению {target_value} ± {tolerance}%. "
            f"Проверено {scenarios_tested} сценариев за {processing_time:.2f} сек.\n"
            f"Найденные значения входных параметров:\n{detail_str}"
            f"{range_description}"
        )
    else:
        return (
            f"Не найдено сценариев, приводящих к целевому значению {target_value} ± {tolerance}%. "
            f"Проверено {scenarios_tested} сценариев за {processing_time:.2f} сек."
            f"{range_description}"
        )


# @tool(args_schema=ExcelAnalysisToolArgs)
def analyze_excel_model(
    file_name: str,
    input_names: list,
    output_names: list,
    output_years: list,
    ranges: list,
    steps: Optional[list] = None,
    user_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> ExcelAnalysisToolResult:
    """
    ГЛАВНЫЙ инструмент для сценарного анализа «что-если».
    Изменяет входные параметры (Inputs), пересчитывает модель (LibreOffice Calc),
    возвращает значения выходных показателей (Outputs) для каждого сценария.
    Пример: "Проанализируй модель при цене метанола от 450 до 500 с шагом 5 и инфляции USD CPI от 0.1 до 0.2 с шагом 0.1, покажи debt/ebitda 2025, net debt/ebitda (ltm) 2025 и icr corr (ltm) 2025"

    Args:
        file_name (str): Название Excel файла для анализа
        input_names (list): Список описательных запросов для идентификации ячеек на листе 'inputs'
        output_names (list): Список описательных запросов для идентификации ячеек на листе 'outputs'
        output_years (list): Список лет для каждого выходного параметра
        ranges (list): Диапазоны [начало, конец] для каждой ячейки в input_names
        steps (list): Шаги для генерации значений (по умолчанию [0.5])
    """
    if steps is None:
        steps = [0.5]
    try:
        # Setup Excel — try user's uploaded file first, fall back to file_name from LLM
        file_path = None
        if user_id:
            stored_name = get_store_file(user_id)
            logger.info(f"Stored name: {stored_name}")
            if stored_name:
                file_path = os.path.abspath(os.path.join(TEMP_DIR, stored_name))
                logger.info(f"File path: {file_path}")
        if not file_path or not os.path.exists(file_path):
            file_path = os.path.abspath(os.path.join(TEMP_DIR, file_name))
            logger.info(f"Constructed file path: {file_path}")
        if not os.path.exists(file_path):
            logger.error(f"Файл {file_name} не найден")
            return ExcelAnalysisToolResult(status="ERROR", result=f"Файл {file_name} не найден", content={})

        # Check analysis cache (LLMs often repeat identical queries)
        cache_key = (
            file_path,
            tuple(input_names),
            tuple(output_names),
            tuple(output_years),
            tuple(tuple(r) for r in ranges),
            tuple(steps),
            user_id,
        )
        cached = _ANALYSIS_CACHE.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for analyze_excel_model — returning cached result")
            return cached

        start_time = time.perf_counter()
        with ExcelWorkbook(file_path) as xl:
            input_mapping = create_input_mapping(xl.get_all_data("Inputs"))
            output_mapping = create_output_mapping(xl.get_all_data("Outputs"))

            # Find and validate output cells
            output_cells = {}
            output_years_dict = {}
            for i, name in enumerate(output_names):
                try:
                    output_info = find_matching_outputs(name, output_mapping)
                    if not output_info:
                        return ExcelAnalysisToolResult(
                            status="ERROR", result=f'Выходной параметр "{name}" не найден', content={}
                        )

                    # Use the year from output_years parameter instead of extracting from name
                    if i < len(output_years):
                        target_year = output_years[i]
                    elif output_years:
                        # Pad with last known year if fewer years than outputs
                        target_year = output_years[-1]
                    else:
                        # Fallback: extract year from query if output_years is not provided
                        target_year = extract_year_from_query(name)
                        if not target_year:
                            return ExcelAnalysisToolResult(
                                status="ERROR",
                                result=f'Не удалось определить год из параметра "{name}"',
                                content={},
                            )

                    actual_output_name = list(output_info.keys())[0]
                    output_cell_ref = get_output_cell_ref(output_mapping, actual_output_name, target_year)
                    output_cells[name] = {
                        "cell_ref": output_cell_ref,
                        "original_name": actual_output_name,
                        "year": target_year,
                    }
                    output_years_dict[name] = target_year
                    out_val = xl.get_cell("Outputs", output_cell_ref)
                    logger.info(
                        "Found output cell for {}: {} at {} = {}", name, actual_output_name, output_cell_ref, out_val
                    )
                    if out_val is None:
                        logger.warning(
                            "Output cell {}: {} at {} returned None (cell exists but no formula)",
                            name,
                            actual_output_name,
                            output_cell_ref,
                        )
                except Exception as e:
                    return ExcelAnalysisToolResult(
                        status="ERROR",
                        result=f'Ошибка при поиске выходного параметра "{name}": {e!s}',
                        content={},
                    )

            # Find and validate input cells (use first output year as default if not found)
            input_cells = {}
            for i, name in enumerate(input_names):
                try:
                    # Use the year of the first output as default if input year is missing
                    default_year = list(output_years_dict.values())[0] if output_years_dict else None
                    cell_address, original_name = find_matching_cell(name, input_mapping, default_year=default_year)
                    input_cells[name] = {
                        "cell_ref": cell_address,
                        "original_name": original_name,
                        "range": ranges[i],
                        "step": steps[i] if i < len(steps) else steps[-1],
                    }
                    inval = xl.get_cell("Inputs", cell_address)
                    logger.info(f"Found input cell for {name}: {cell_address} = {inval}")
                except ValueError as e:
                    return ExcelAnalysisToolResult(
                        status="ERROR",
                        result=f'Не удалось найти ячейку для параметра "{name}": {e!s}',
                        content={},
                    )

            # Generate value sets for each input
            value_sets = []
            for name in input_names:
                info = input_cells[name]
                start, end = info["range"]
                step = info["step"]
                values = np.arange(start, end + 1e-10, step)
                value_sets.append(values.tolist())

            # Проверяем количество комбинаций до генерации
            from math import prod as math_prod

            n_combinations = math_prod(len(vs) for vs in value_sets)
            if n_combinations > MAX_SCENARIOS:
                n_per_input = {name: len(vs) for name, vs in zip(input_names, value_sets)}
                return ExcelAnalysisToolResult(
                    status="ERROR",
                    result=(
                        f"Диапазон слишком широкий: будет проанализировано {n_combinations:,} сценариев "
                        f"(максимум {MAX_SCENARIOS:,}).\n\n"
                        f"Комбинации на входные параметры:\n"
                        + "\n".join(f"  - {name}: {n_vals} значений" for name, n_vals in n_per_input.items())
                        + "\n\nУменьшите диапазон или увеличьте шаг."
                    ),
                    content={},
                )

            # Generate all combinations
            combinations = list(product(*value_sets))
            logger.info("Генерируем {} сценариев для анализа", len(combinations))

            logger.info(
                "Excel calculation started: file={}, inputs={}, outputs={}, scenarios={}",
                os.path.basename(file_path),
                len(input_names),
                len(output_names),
                len(combinations),
            )

            # Test scenarios using compiled function (fast, no LibreOffice)
            fname = os.path.basename(file_path)
            input_refs = [f"'[{fname}]INPUTS'!{input_cells[n]['cell_ref']}" for n in input_names]
            output_refs = [f"'[{fname}]OUTPUTS'!{output_cells[n]['cell_ref']}" for n in output_names]
            func = xl.get_compiled_func(input_refs, output_refs)

            results = {"inputs": [], "outputs": []}
            for values in combinations:
                # Evaluate via compiled function (all inputs are set inside, all
                # outputs are returned at once). No separate set_cell: with the
                # LibreOffice backend a redundant write doubles the IPC cost.
                try:
                    raw = func(*values)
                    if not isinstance(raw, (list, tuple)):
                        raw = (raw,)
                except Exception:
                    continue

                current_inputs = {}
                for name, value in zip(input_names, values, strict=True):
                    current_inputs[input_cells[name]["original_name"]] = value

                current_outputs = {}
                for idx, (name, info) in enumerate(output_cells.items()):
                    output_key = f"{name}_{info['year']}"
                    try:
                        v = _formula_scalar(raw[idx])
                        current_outputs[output_key] = round(float(v), 3) if v is not None else None
                    except (ValueError, TypeError):
                        current_outputs[output_key] = None

                results["inputs"].append(current_inputs)
                results["outputs"].append(current_outputs)

            processing_time = time.perf_counter() - start_time

            logger.info(
                "Excel calculation completed: elapsed={}s, scenarios={}",
                round(processing_time, 2),
                len(combinations),
            )

            # Convert results to DataFrame for easier handling
            df_outputs = pd.DataFrame(results["outputs"])
            df_inputs = pd.DataFrame(results["inputs"])

            # --- Save all scenario results to Excel ---
            output_dir = f"{TEMP_DIR}/excel_analysis"
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            scenario_file = f"{output_dir}/scenarios_{timestamp}.xlsx"
            result_df = pd.concat([df_inputs, df_outputs], axis=1)
            result_df.to_excel(scenario_file, index=False)

            # --- Save scenario matrix and min/max matrix if possible ---
            scenario_matrix_file = None
            minmax_matrix_file = None
            if len(input_names) == 2:
                try:
                    scenario_matrix_file = create_scenario_matrix(df_inputs, df_outputs)
                    logger.info(f"Сохранена матрица сценариев: {scenario_matrix_file}")
                except Exception as e:
                    logger.info(f"Не получилось сохранить матрицу сценариев: {e}")
            else:
                logger.info("Число входных inputs больше двух, не сохраняю матрицу сценариев")
            try:
                minmax_matrix_file = generate_min_max_scenarios(df_inputs, df_outputs)
                logger.info(f"Сохранена матрица min/max сценариев: {minmax_matrix_file}")
            except Exception as e:
                logger.info(f"Не получилось сохранить матрицу min/max сценариев: {e}")

            # Add file paths to results_dict
            results_dict = {
                "scenario_file": scenario_file,
                "scenario_matrix_file": scenario_matrix_file,
                "minmax_matrix_file": minmax_matrix_file,
                "result_df": result_df,
            }

            # Format results as readable text table for the LLM
            if len(combinations) <= 50:
                table_lines = []
                table_lines.append("Результаты анализа по сценариям:")
                table_lines.append("")
                # Build header
                headers = list(results["inputs"][0].keys()) + list(results["outputs"][0].keys())
                table_lines.append(" | ".join(headers))
                table_lines.append("-" * (sum(len(h) for h in headers) + 3 * (len(headers) - 1)))
                for inp, out in zip(results["inputs"], results["outputs"], strict=True):
                    row = [str(v) for v in inp.values()]
                    row += [str(round(v, 3) if isinstance(v, float) else v) for v in out.values()]
                    table_lines.append(" | ".join(row))
                result_text = "\n".join(table_lines)
            else:
                # Too many scenarios — show a sample
                sample_lines = []
                sample_lines.append(f"Всего проанализировано {len(combinations)} сценариев. Показаны первые 5:")
                sample_lines.append("")
                headers = list(results["inputs"][0].keys()) + list(results["outputs"][0].keys())
                sample_lines.append(" | ".join(headers))
                sample_lines.append("-" * (sum(len(h) for h in headers) + 3 * (len(headers) - 1)))
                for inp, out in zip(results["inputs"][:5], results["outputs"][:5], strict=True):
                    row = [str(v) for v in inp.values()]
                    row += [str(round(v, 3) if isinstance(v, float) else v) for v in out.values()]
                    sample_lines.append(" | ".join(row))
                sample_lines.append("...")
                result_text = "\n".join(sample_lines)

            result = ExcelAnalysisToolResult(
                status="OK",
                result=result_text,
                content=results_dict,
            )

            # Cache successful result
            _ANALYSIS_CACHE[cache_key] = result
            _ANALYSIS_CACHE_ORDER.append(cache_key)
            if len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_MAXSIZE:
                oldest = _ANALYSIS_CACHE_ORDER.pop(0)
                _ANALYSIS_CACHE.pop(oldest, None)

            return result

    except Exception as e:
        logger.opt(exception=True).error("Ошибка при анализе Excel модели: {}", str(e))
        msg = _humanize_error(e)
        return ExcelAnalysisToolResult(status="ERROR", result=f"Ошибка при анализе: {msg}", content={})


def save_analysis_results(
    scenarios: list,
    optimized_scenario: dict,
    input_names: list,
    output_name: str,
    target_value: float,
    tolerance: float,
    search_config: dict,
) -> str:
    """Save analysis results to Excel file.

    Args:
        scenarios: List of all tested scenarios
        optimized_scenario: The optimized scenario if found
        input_names: List of input parameter names
        output_name: Name of the target output
        target_value: Target value to achieve
        tolerance: Allowed tolerance percentage
        search_config: Search configuration details

    Returns:
        str: Path to the saved Excel file
    """
    try:
        import pandas as pd

        # Create output directory
        output_dir = f"{TEMP_DIR}/excel_analysis"
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_file = f"{output_dir}/scenarios_analysis_{timestamp}.xlsx"

        # Create DataFrame from scenarios
        scenario_rows = []
        for scenario in scenarios:
            row = {
                **{name: scenario["input_values"].get(name) for name in input_names},
                "output_value": scenario["output_value"],
                "deviation": scenario["deviation"],
                "deviation_percent": scenario["deviation_percent"],
            }
            scenario_rows.append(row)

        df = pd.DataFrame(scenario_rows)

        # Add optimized scenario if available
        if optimized_scenario:
            opt_row = {
                **{name: optimized_scenario["input_values"].get(name) for name in input_names},
                "output_value": optimized_scenario["actual_output"],
                "deviation": optimized_scenario["deviation"],
                "deviation_percent": optimized_scenario["deviation_percent"],
            }
            df_opt = pd.DataFrame([opt_row])
            df = pd.concat([df_opt, df], ignore_index=True)

        # Save to Excel with formatting
        with pd.ExcelWriter(excel_file, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Scenarios", index=False)
            workbook = writer.book
            worksheet = writer.sheets["Scenarios"]

            # Add formats
            header_format = workbook.add_format(
                {"bold": True, "text_wrap": True, "valign": "top", "bg_color": "#D9D9D9", "border": 1}
            )

            # Write headers with format
            for col_num, value in enumerate(df.columns.values):
                worksheet.write(0, col_num, value, header_format)
                worksheet.set_column(col_num, col_num, 15)

            # Add configuration sheet
            config_data = [
                ["Analysis Configuration"],
                ["Target Output", output_name],
                ["Target Value", target_value],
                ["Tolerance", f"{tolerance}%"],
                [""],
                ["Input Ranges:"],
            ]

            for name in input_names:
                if name in search_config["input_ranges"]:
                    range_data = search_config["input_ranges"][name]
                    config_data.append(
                        [name, f"Min: {round(range_data['min'], 3)}", f"Max: {round(range_data['max'], 3)}"]
                    )

            config_df = pd.DataFrame(config_data)
            config_df.to_excel(writer, sheet_name="Configuration", index=False, header=False)

        logger.info(f"Сохранен файл с результатами анализа: {excel_file}")
        return excel_file

    except Exception as e:
        logger.warning(f"Ошибка при сохранении результатов в Excel: {e!s}")
        return None


def extract_year_from_query(query: str) -> int:
    """Extract year from a query string."""
    import re

    year_match = re.search(r"20\d{2}", query)
    if year_match:
        return int(year_match.group())
    return None


def normalize_text(text: str) -> str:
    """Normalize text for better matching, including Russian stemming.

    Args:
        text: Text to normalize

    Returns:
        str: Normalized text with stemmed Russian words
    """
    # Convert to lowercase
    text = text.lower()

    # Remove special characters and extra spaces
    text = re.sub(r"[^\w\s]", " ", text)

    # Stem each word
    words = text.split()
    stemmed_words = [russian_stem(word) for word in words]

    # Join words back together
    text = " ".join(stemmed_words)

    # Normalize spaces
    text = " ".join(text.split())

    return text


def create_input_mapping(data: list[list]) -> dict:
    """Create a structured mapping of input descriptions to their cells.

    *data* is a 2‑D list returned by :meth:`ExcelWorkbook.get_all_data`.
    """
    try:
        if not data:
            raise ValueError("No data found in inputs sheet")

        # First row contains headers
        headers = data[0]

        # Find the column index for variable names (column F)
        name_col_idx = None
        for i, header in enumerate(headers):
            if header == "Наименование":
                name_col_idx = i
                break

        if name_col_idx is None:
            raise ValueError("Could not find 'Наименование' column")

        # Find year columns
        year_columns = {}
        for i, header in enumerate(headers):
            try:
                year = float(header)
                if 2000 <= year <= 2100:
                    year_columns[i] = int(year)
            except (ValueError, TypeError):
                continue

        if not year_columns:
            raise ValueError("No year columns found in headers")

        # logger.info(f"Found year columns: {year_columns}")  # Commented out

        # Create mapping of row descriptions to their values
        row_mapping = {}
        for row_idx, row in enumerate(data[1:], start=2):  # Start from row 2 (after headers)
            if not row:  # Skip completely empty rows
                # logger.debug(f"Skipping empty row {row_idx}")  # Commented out
                continue

            # Ensure row has enough columns
            if len(row) <= name_col_idx:
                # logger.debug(f"Row {row_idx} too short: {row}")  # Commented out
                continue

            # Get the variable name from column F
            description = str(row[name_col_idx]).strip() if row[name_col_idx] is not None else ""
            if not description:
                # logger.debug(f"Empty description in row {row_idx}")  # Commented out
                continue

            # Store the normalized description and its original form
            normalized_desc = normalize_text(description)

            # Get values for each year column
            values = {}
            for col_idx, year in year_columns.items():
                if col_idx < len(row):
                    value = row[col_idx]
                    if value is not None:
                        try:
                            # Try to convert to float if possible
                            values[year] = float(value)
                        except (ValueError, TypeError):
                            values[year] = value

            row_mapping[normalized_desc] = {"original": description, "row": row_idx, "values": values}

        return {"year_columns": year_columns, "row_mapping": row_mapping}

    except Exception as e:
        logger.opt(exception=True).error("Error in create_input_mapping: {}", str(e))
        raise


def jaccard_similarity(str1: str, str2: str) -> float:
    """Calculate Jaccard similarity between two strings.

    Args:
        str1: First string to compare
        str2: Second string to compare

    Returns:
        float: Jaccard similarity score between 0 and 1
    """
    # Convert strings to sets of words
    set1 = set(str1.lower().split())
    set2 = set(str2.lower().split())

    # Calculate intersection and union
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))

    # Return Jaccard similarity
    return intersection / union if union > 0 else 0.0


def find_matching_cell(
    query: str,
    input_mapping: dict,
    default_year: int = None,
    return_best_score: bool = False,
) -> tuple:
    """Find the single most matching cell for a given query using Jaccard similarity. If year is not found, use default_year if provided."""
    # Extract year from query
    target_year = extract_year_from_query(query)
    if not target_year and default_year:
        target_year = default_year
    if not target_year:
        raise ValueError(f"No year found in query: {query} and no default year provided.")

    # Normalize query without the year
    normalized_query = normalize_text(query.replace(str(target_year), "").strip())

    # Find best matching row based only on description
    best_match = None
    best_score = 0

    for norm_desc, row_info in input_mapping["row_mapping"].items():
        # Calculate Jaccard similarity score
        score = jaccard_similarity(normalized_query, norm_desc)

        if score > best_score:
            best_score = score
            best_match = row_info

    if not best_match or best_score < 0.1:  # Very low threshold since we want the best match even if not great
        raise ValueError(f"No match found for query: {query}")

    # Find the column index for the target year
    year_col_idx = None
    for col_idx, year in input_mapping["year_columns"].items():
        if year == target_year:
            year_col_idx = col_idx
            break

    if year_col_idx is None:
        raise ValueError(f"Year {target_year} not found in headers")

    # Convert to Excel column letter (handling columns beyond Z)
    col_letter = ""
    while year_col_idx >= 0:
        col_letter = chr(ord("A") + (year_col_idx % 26)) + col_letter
        year_col_idx = year_col_idx // 26 - 1

    # Return proper xlwings cell reference format
    cell_reference = f"{col_letter}{best_match['row']}"
    if return_best_score:
        return cell_reference, best_match["original"], best_score
    return cell_reference, best_match["original"]


def create_output_mapping(data: list[list]) -> dict:
    """Create a structured mapping of output descriptions to their cells.

    *data* is a 2‑D list returned by :meth:`ExcelWorkbook.get_all_data`.
    """
    try:
        if not data:
            raise ValueError("No data found in outputs sheet")

        logger.info(f"Output data structure: {len(data)} rows")
        if data:
            logger.info(f"First row: {data[0]}")

        # First row contains headers
        headers = data[0]
        logger.info(f"Output headers: {headers}")

        # Find the column index for variable names (column F)
        name_col_idx = None
        for i, header in enumerate(headers):
            if header == "Наименование":
                name_col_idx = i
                break

        if name_col_idx is None:
            raise ValueError("Could not find 'Наименование' column")

        logger.info(f"Found 'Наименование' column at index: {name_col_idx}")

        # Find year columns
        year_columns = {}
        for i, header in enumerate(headers):
            try:
                year = float(header)
                if 2000 <= year <= 2100:
                    year_columns[i] = int(year)
            except (ValueError, TypeError):
                continue

        if not year_columns:
            raise ValueError("No year columns found in headers")

        logger.info(f"Found year columns: {year_columns}")

        # Create mapping of output descriptions to their values
        output_mapping = {}
        for row_idx, row in enumerate(data[1:], start=2):  # Start from row 2 (after headers)
            if not row:  # Skip completely empty rows
                logger.debug(f"Skipping empty row {row_idx}")
                continue

            # Ensure row has enough columns
            if len(row) <= name_col_idx:
                logger.debug(f"Row {row_idx} too short: {row}")
                continue

            # Get the variable name from column F
            description = str(row[name_col_idx]).strip() if row[name_col_idx] is not None else ""
            if not description:
                logger.debug(f"Empty description in row {row_idx}")
                continue

            # Store the normalized description and its original form
            normalized_desc = normalize_text(description)

            # Get values for each year column
            values = {}
            for col_idx, year in year_columns.items():
                if col_idx < len(row):
                    value = row[col_idx]
                    if value is not None:
                        try:
                            # Try to convert to float if possible
                            values[year] = float(value)
                        except (ValueError, TypeError):
                            values[year] = value

            output_mapping[normalized_desc] = {"original": description, "row": row_idx, "values": values}

            if len(output_mapping) <= 5:  # Log first 5 entries for debugging
                logger.info(f"Added mapping for: {description} (normalized: {normalized_desc})")
                logger.info(f"Values: {values}")

        logger.info(f"Created output mapping with {len(output_mapping)} entries")
        return {"year_columns": year_columns, "output_mapping": output_mapping}

    except Exception as e:
        logger.opt(exception=True).error("Error in create_output_mapping: {}", str(e))
        raise


def find_matching_outputs(query: str, output_mapping: dict, return_best_score: bool = False) -> dict:
    """Find the single most matching output for a given query using Jaccard similarity."""
    # Extract year from query if present
    target_year = extract_year_from_query(query)

    # Normalize query
    normalized_query = normalize_text(query)

    # Find best matching output
    best_match = None
    best_score = 0

    for norm_desc, output_info in output_mapping["output_mapping"].items():
        # Calculate Jaccard similarity score
        score = jaccard_similarity(normalized_query, norm_desc)

        if score > best_score:
            best_score = score
            best_match = (output_info["original"], output_info)

    if not best_match or best_score < 0.1:  # Very low threshold since we want the best match even if not great
        return {"_best_score": best_score} if return_best_score else {}

    output_name, output_info = best_match

    # If year is specified, only include that year's value
    if target_year:
        if target_year in output_info["values"]:
            result = {output_name: {"value": output_info["values"][target_year], "year": target_year}}
            if return_best_score:
                result["_best_score"] = best_score
            return result
    else:
        # Include all years if no specific year requested
        result = {output_name: {"values": output_info["values"]}}
        if return_best_score:
            result["_best_score"] = best_score
        return result

    return {"_best_score": best_score} if return_best_score else {}


def generate_min_max_scenarios(inputs: pd.DataFrame, outputs: pd.DataFrame) -> str:
    """
    Генерирует все комбинации min/max значений параметров и находит соответствующие результаты
    Возвращает путь к сохраненному файлу Excel.
    """
    # Проверка данных
    if len(inputs) != len(outputs):
        raise ValueError("Длины датафреймов должны совпадать")

    # Получаем границы для каждого параметра
    bounds = {col: (inputs[col].min(), inputs[col].max()) for col in inputs.columns}

    # Генерируем все возможные комбинации min/max
    from itertools import product

    combinations = list(product(*[(bounds[col][0], bounds[col][1]) for col in inputs.columns]))

    # Объединяем данные
    combined = pd.concat([inputs, outputs], axis=1)

    # Фильтруем строки по комбинациям
    result = pd.DataFrame()
    for combo in combinations:
        # Формируем условие фильтрации
        mask = pd.Series([True] * len(combined))
        for idx, col in enumerate(inputs.columns):
            mask &= combined[col] == combo[idx]

        # Добавляем найденные строки
        result = pd.concat([result, combined[mask]], ignore_index=True)

    # Создаем папку
    # output_dir = "excel_scripts"
    output_dir = f"{TEMP_DIR}/excel_analysis"
    os.makedirs(output_dir, exist_ok=True)

    # Сохраняем в Excel
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"min_max_scenarios_{timestamp}.xlsx"
    filepath = os.path.join(output_dir, filename)

    with pd.ExcelWriter(filepath, engine="xlsxwriter") as writer:
        result.to_excel(writer, sheet_name="Сценарии", index=False)
        workbook = writer.book
        worksheet = writer.sheets["Сценарии"]
        for idx, col in enumerate(result.columns):
            worksheet.set_column(idx + 1, idx + 1, 15)
    return filepath


def create_scenario_matrix(inputs: pd.DataFrame, outputs: pd.DataFrame) -> str:
    """
    Создает матрицу сценариев в Excel файле и возвращает путь к файлу.
    :param inputs: Датафрейм с двумя столбцами (ось строк и столбцов матрицы)
    :param outputs: Датафрейм с произвольным количеством столбцов (значения матрицы)
    :return: str (путь к Excel файлу)
    """
    # Проверка входных данных
    if len(inputs.columns) != 2:
        raise ValueError("Inputs dataframe must have exactly 2 columns")

    if len(inputs) != len(outputs):
        raise ValueError("Inputs and outputs dataframes must have the same length")

    # Создаем папку для результатов
    # output_dir = "excel_scripts"
    output_dir = f"{TEMP_DIR}/excel_analysis"
    os.makedirs(output_dir, exist_ok=True)

    # Генерируем имя файла
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"scenarios_matrix_{timestamp}.xlsx"
    filepath = os.path.join(output_dir, filename)

    # Получаем названия осей
    row_axis, col_axis = inputs.columns

    with ExcelWriter(filepath, engine="xlsxwriter") as writer:
        for output_col in outputs.columns:
            try:
                # Создаем матрицу
                matrix = inputs.join(outputs[output_col]).pivot_table(
                    index=row_axis, columns=col_axis, values=output_col, aggfunc="first"
                )

                # Создаем лист
                sheet_name = str(output_col)[:31].translate({ord(c): None for c in "[]:*?/\\"})

                # Записываем матрицу
                matrix.to_excel(writer, sheet_name=sheet_name, startrow=1)
                # print(len(matrix.columns))
                # Работаем с листом Excel
                workbook = writer.book
                worksheet = writer.sheets[sheet_name]

                # Добавляем объединенный заголовок
                header_format = workbook.add_format(
                    {"bold": True, "border": 1, "align": "center", "valign": "vcenter"}
                )

                # Объединяем ячейки для главного заголовка
                worksheet.merge_range(f"B1:{chr(65 + len(matrix.columns))}1", col_axis, header_format)

                # Форматирование подзаголовков
                subheader_format = workbook.add_format({"bold": True, "border": 1, "align": "center"})

                # Автоподбор ширины
                worksheet.set_column(0, 0, 20)  # Первый столбец
                for idx, col in enumerate(matrix.columns):
                    worksheet.set_column(idx + 1, idx + 1, 10)

            except Exception as e:
                logger.error(f"Ошибка в {output_col}: {e!s}")
    return filepath


def get_store_file(user_id: str) -> str:
    store = APP_CTX.agent_memory.store

    namespace = ("memories", user_id)
    key = user_id
    stored_value = store.get(namespace, key)

    # print(f'namespace: {namespace}')
    # print(f'key: {key}')
    # print(f'stored_value: {stored_value}')

    if stored_value and hasattr(stored_value, "value"):
        file_path = stored_value.value.get("filename")
        # print(f'FILE_PATH {file_path}')
    else:
        return

    return file_path
