"""Model index from the uploaded workbook: IDs, names, input values by year, output years (spec §2 «Индекс модели»)."""

from aigw_service.api.v1.subagents.utils import build_mappings
from aigw_service.api.v2.tools.context import ModelIndex


def _numeric(values: dict) -> dict[int, float]:
    return {int(year): float(v) for year, v in (values or {}).items() if isinstance(v, (int, float))}


def build_model_index(path: str) -> ModelIndex:
    """One openpyxl pass; IDs are numbered in the same order as ``_build_catalog_with_ids`` (IN0…, OUT0…)."""
    input_mapping, output_mapping = build_mappings(path)
    inputs, input_values = {}, {}
    for idx, info in enumerate(input_mapping["row_mapping"].values()):
        inputs[f"IN{idx}"] = info["original"].strip()
        input_values[f"IN{idx}"] = _numeric(info.get("values"))
    outputs, output_years = {}, {}
    for idx, info in enumerate(output_mapping["output_mapping"].values()):
        outputs[f"OUT{idx}"] = info["original"].strip()
        output_years[f"OUT{idx}"] = tuple(sorted(_numeric(info.get("values"))))
    return ModelIndex(inputs, outputs, input_values, output_years)
