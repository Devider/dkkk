"""Model card: what the model always sees about the uploaded workbook (spec §9 «Слои контекста»)."""

from typing import Any

from aigw_service.api.v2.tools.context import ModelIndex

KEY_METRIC_MARKERS = (
    "DSCR",
    "LLCR",
    "ICR",
    "Debt/EBITDA",
    "Чистый долг",
    "EBITDA",
    "Выручка",
    "Чистая прибыль",
    "FCF",
)
MAX_KEY_METRICS = 12
PER_MARKER = 2  # several DSCR / ICR variants must not crowd out EBITDA, revenue, profit


def build_card(file_name: str, index: ModelIndex) -> dict[str, Any]:
    """Counts, forecast years and lender metrics that have values; built once per workbook (registry)."""
    years = sorted({y for ys in index.output_years.values() for y in ys})
    metrics: list[str] = []
    for marker in KEY_METRIC_MARKERS:
        found = [
            name
            for output_id, name in index.outputs.items()
            if marker.lower() in name.lower() and index.output_years.get(output_id) and name not in metrics
        ]
        metrics += found[:PER_MARKER]
    return {
        "file_name": file_name,
        "inputs": len(index.inputs),
        "outputs": len(index.outputs),
        "years": f"{years[0]}–{years[-1]}" if years else "",
        "key_metrics": metrics[:MAX_KEY_METRICS],
    }


CARD_HEADER = "## Загруженная модель"
"""First line of the card block; ContextMeter finds the card layer by it."""


def card_text(card: dict[str, Any], replaced: bool = False) -> str:
    """Prompt block; metric names are data from the workbook, not instructions."""
    lines = [
        CARD_HEADER,
        f"Файл: {card['file_name']}. Входов: {card['inputs']}, выходов: {card['outputs']}.",
    ]
    if card.get("years"):
        lines.append(f"Годы со значениями выходов: {card['years']}.")
    if card.get("key_metrics"):
        lines.append("Ключевые метрики кредитора в модели: " + "; ".join(card["key_metrics"]) + ".")
    if replaced:
        lines.append(
            "Модель заменена в этом ходе: прежние результаты и цифры относятся к другой книге, не используй их."
        )
    return "\n".join(lines)
