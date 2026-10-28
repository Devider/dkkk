"""``trace_dependencies``: inputs an output depends on through the workbook formulas (story S3.4, design D3).

A skill tool opened by ``sensitivity-drivers``; no recalculation engine. Deviation from §10 «Оборачивает»: v1
``build_dependency_graph`` finds the output by text, skips ranges and draws a PNG; this tool takes an index ID and
walks the formulas with ``excel_engine.dependencies.trace``. Rows map to IDs with the same functions as the model
index, so ``IN…`` / ``OUT…`` here are the IDs the model already has.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import openpyxl
from pydantic import BaseModel, Field

from aigw_service.api.v1.tools import normalize_text
from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import ToolContext
from aigw_service.api.v2.tools.envelope import Number, ToolSuccess
from aigw_service.api.v2.tools.index_builder import create_input_mapping, create_output_mapping, sheet_rows
from aigw_service.api.v2.tools.prechecks import output_has_values, require, year_in_horizon
from excel_engine.dependencies import MAX_TRACE_CELLS, column_letter, trace

INPUTS_SHEET, OUTPUTS_SHEET = "Inputs", "Outputs"
NAME_HEADER = "Наименование"
SUMMARY_INPUTS = 10
CACHED_WORKBOOKS = 2
"""Workbooks with formulas kept in memory: loading a large model takes seconds, the agent repeats calls."""

TRUNCATED_NOTE = f"Связи показаны не полностью: обход остановлен после {MAX_TRACE_CELLS} ячеек."
SHADOW_NOTE = (
    "Название входа повторяется в листе Inputs, и связь идёт через другую строку, чем меняет пересчёт: {}. "
    "Сетка по этому входу может не изменить показатель."
)
SKIPPED_NOTE = "Часть ссылок не прослежена (другие книги, именованные диапазоны, целые столбцы): {} шт."
NEXT_HINT = (
    "Базовые значения входов — get_values. Для рейтинга влияния — отдельная сетка ±10 % по каждому входу "
    "и derive elasticity."
)


class TraceArgs(BaseModel):
    """Arguments of trace_dependencies the model chooses."""

    output_id: str | None = Field(default=None, description="ID выхода (OUT…) из resolve_terms")
    year: int | None = Field(default=None, description="Год, за который смотреть связи; только из запроса")


@dataclass(frozen=True)
class CellMap:
    """Rows of the Inputs / Outputs sheets by index ID and the column of each year on the Outputs sheet.

    ``input_ids`` / ``output_ids`` give every named row the ID of its name; a name repeated on several rows keeps one
    ID, and the index (like a recalculation) uses only one of those rows — ``input_rows`` (design D3, found in S3.4).
    """

    input_ids: dict[int, str]
    input_rows: dict[str, int]
    output_ids: dict[int, str]
    output_rows: dict[str, int]
    year_columns: dict[int, str]


def _ids_by_row(rows: list[list[Any]], mapping: dict[str, dict], prefix: str) -> tuple[dict[int, str], dict[str, int]]:
    """Every row whose name normalizes to a mapping key gets that key's ID; the mapping's own row is the index row."""
    ids = {key: f"{prefix}{i}" for i, key in enumerate(mapping)}
    own = {ids[key]: info["row"] for key, info in mapping.items()}
    column = rows[0].index(NAME_HEADER)
    by_row = {}
    for number, line in enumerate(rows[1:], start=2):
        name = line[column] if column < len(line) else None
        item_id = ids.get(normalize_text(str(name).strip())) if name is not None and str(name).strip() else None
        if item_id is not None:
            by_row[number] = item_id
    return by_row, own


def cell_map(path: str) -> CellMap:
    """One cached-values pass with the mapping functions of ``build_model_index``: same numbering of IDs."""
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        input_rows, output_rows = sheet_rows(workbook, INPUTS_SHEET), sheet_rows(workbook, OUTPUTS_SHEET)
    finally:
        workbook.close()
    inputs, outputs = create_input_mapping(input_rows), create_output_mapping(output_rows)
    input_ids, input_own = _ids_by_row(input_rows, inputs["row_mapping"], "IN")
    output_ids, output_own = _ids_by_row(output_rows, outputs["output_mapping"], "OUT")
    years = {year: column_letter(col) for col, year in outputs["year_columns"].items()}
    return CellMap(input_ids, input_own, output_ids, output_own, years)


@lru_cache(maxsize=CACHED_WORKBOOKS)
def _load(path: str, mtime_ns: int, size: int) -> tuple[Any, CellMap]:  # pylint: disable=unused-argument
    """Key includes the file's mtime and size: a re-uploaded workbook is loaded again. Only read afterwards."""
    return openpyxl.load_workbook(path, data_only=False), cell_map(path)


def load(path: str) -> tuple[Any, CellMap]:
    stat = os.stat(path)
    return _load(path, stat.st_mtime_ns, stat.st_size)


def _items(depths: dict[str, int], name: Any) -> list[dict[str, Any]]:
    ordered = sorted(depths.items(), key=lambda kv: (kv[1], int(kv[0].lstrip("INOUT"))))
    return [{"id": item_id, "name": name(item_id), "depth": depth} for item_id, depth in ordered]


def _rows_of(rows: dict[tuple[str, int], int], sheet: str) -> dict[int, int]:
    return {row: depth for (title, row), depth in rows.items() if title.lower() == sheet.lower()}


def _depths(reached: dict[int, int], ids: dict[int, str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for row, depth in reached.items():
        item_id = ids.get(row)
        if item_id is not None and depth < found.get(item_id, depth + 1):
            found[item_id] = depth
    return found


def _shadowed(reached: dict[int, int], cells: CellMap) -> list[str]:
    """Inputs reached only through a repeated row the recalculation does not change."""
    via = {cells.input_ids[r] for r in reached if r in cells.input_ids}
    own = {
        cells.input_ids[r] for r in reached if r in cells.input_ids and cells.input_rows.get(cells.input_ids[r]) == r
    }
    return sorted(via - own, key=lambda i: int(i[2:]))


def trace_dependencies(args: TraceArgs, ctx: ToolContext) -> ToolSuccess:
    index = ctx.model_index
    require(([] if args.output_id else ["ID выхода"]) + ([] if args.year is not None else ["год"]))
    output_id, year = str(args.output_id), int(args.year)  # type: ignore[arg-type]  # checked by require
    name = index.output_name(output_id)
    output_has_values(index, output_id)
    year_in_horizon(index, output_id, year)
    workbook, cells = load(ctx.file_path)
    start = (OUTPUTS_SHEET, f"{cells.year_columns[year]}{cells.output_rows[output_id]}")
    walk = trace(workbook, start, [OUTPUTS_SHEET], [INPUTS_SHEET])
    input_rows = _rows_of(walk.rows, INPUTS_SHEET)
    inputs = _items(_depths(input_rows, cells.input_ids), index.input_name)
    outputs_depths = _depths(_rows_of(walk.rows, OUTPUTS_SHEET), cells.output_ids)
    outputs_depths.pop(output_id, None)
    outputs = _items(outputs_depths, index.output_name)
    notes = [TRUNCATED_NOTE] if walk.truncated else []
    if shadowed := _shadowed(input_rows, cells):
        notes.append(SHADOW_NOTE.format(", ".join(f"«{index.input_name(i)}»" for i in shadowed)))
    notes += [SKIPPED_NOTE.format(walk.skipped)] if walk.skipped else []
    shown = ", ".join(i["name"] for i in inputs[:SUMMARY_INPUTS])
    more = f" и ещё {len(inputs) - SUMMARY_INPUTS}" if len(inputs) > SUMMARY_INPUTS else ""
    result = (
        f"{name}, {year} зависит от {len(inputs)} входов модели"
        + (f"; ближайшие по формулам: {shown}{more}" if inputs else "")
        + (f". Через выходы: {', '.join(o['name'] for o in outputs[:SUMMARY_INPUTS])}" if outputs else "")
        + "."
    )
    numbers = [
        Number(value=len(inputs), label=f"Входов в связях {name}, {year}"),
        Number(value=walk.visited, label="Пройдено ячеек формул"),
    ]
    data = {"output": {"id": output_id, "name": name, "year": year}, "inputs": inputs, "outputs": outputs}
    data |= {"visited": walk.visited, "truncated": walk.truncated}
    return ToolSuccess(result=result, data=data, numbers=numbers, notes=notes, hint=NEXT_HINT)


TRACE_DEPENDENCIES = ToolSpec(
    name="trace_dependencies",
    description=(
        "Показывает, от каких входов модели зависит выход за год, по формулам книги, без пересчёта. "
        "Вызывай, чтобы найти драйверы показателя или объяснить его изменение."
    ),
    args_schema=TraceArgs,
    call=trace_dependencies,
    availability="sensitivity-drivers",
)
