"""Fake domain tools and synthetic long context for the S0.2 GigaChat spike (no LibreOffice, no workbook)."""

import json
import random

from langchain_core.tools import tool

METRICS = {
    "EBITDA": {2025: 1180.4, 2026: 1265.9, 2027: 1342.7},
    "DSCR": {2025: 1.41, 2026: 1.33, 2027: 1.27},
    "Debt/EBITDA": {2025: 2.85, 2026: 2.71, 2027: 2.64},
}
FX = {2025: 92.4, 2026: 96.8, 2027: 101.3}


@tool
def get_metric(name: str, year: int) -> str:
    """Возвращает значение финансового показателя модели (EBITDA, DSCR, Debt/EBITDA) за год."""
    value = METRICS.get(name, {}).get(year)
    if value is None:
        return json.dumps({"error": "unknown_id", "hint": f"Доступны: {', '.join(METRICS)}; годы 2025–2027"})
    return json.dumps({"result": f"{name} {year} = {value}", "value": value}, ensure_ascii=False)


@tool
def get_fx_rate(year: int) -> str:
    """Возвращает средний курс USD/RUB за год из макропрогноза модели."""
    value = FX.get(year)
    if value is None:
        return json.dumps({"error": "invalid_args", "hint": "Годы 2025–2027"})
    return json.dumps({"result": f"USD/RUB {year} = {value}", "value": value}, ensure_ascii=False)


@tool
def open_skill(name: str) -> str:
    """Открывает skill по имени (например, covenant-headroom) и делает доступными его инструменты."""
    return f"Skill {name} прочитан. Открыт инструмент calc_headroom(metric, threshold, year)."


@tool
def calc_headroom(metric: str, threshold: float, year: int) -> str:
    """Считает запас показателя до порога ковенанты в процентах. Доступен только после open_skill."""
    value = METRICS.get(metric, {}).get(year)
    if value is None:
        return json.dumps({"error": "unknown_id"})
    headroom = round((value - threshold) / threshold * 100, 1)
    return json.dumps({"result": f"Запас {metric} {year}: {headroom} %", "value": headroom}, ensure_ascii=False)


BASE_TOOLS = [get_metric, get_fx_rate]


def scenario_grid(seed: int, rows: int) -> str:
    """Synthetic EMA-style grid: methanol price × output metrics, deterministic per seed."""
    rng = random.Random(seed)
    lines = ["Сценарная сетка: цена метанола (USD/т) → DSCR 2026, Debt/EBITDA 2026, EBITDA 2026 (млн USD)"]
    for i in range(rows):
        price = 250 + i * 2.5
        dscr = round(0.6 + price / 380 + rng.uniform(-0.02, 0.02), 3)
        lev = round(5.2 - price / 190 + rng.uniform(-0.03, 0.03), 3)
        ebitda = round(price * 3.1 + rng.uniform(-8, 8), 1)
        lines.append(f"цена {price:.1f}: DSCR {dscr}; Debt/EBITDA {lev}; EBITDA {ebitda}")
    return "\n".join(lines)


LONG_QUERIES = [
    (
        "Посчитай DSCR и Debt/EBITDA 2026 при падении цены метанола на 10–30 % от базы 420 USD/т, "
        "найди цену, при которой DSCR = 1,2, и оцени, хватает ли запаса по ковенанте DSCR ≥ 1,2."
    ),
    "Как меняется EBITDA 2026 при цене метанола от 300 до 450 USD/т и при какой цене Debt/EBITDA превысит 3,5x?",
    "Оцени запас прочности по ковенантам DSCR ≥ 1,2 и Debt/EBITDA ≤ 3,5 в 2026 году при стрессе цены метанола.",
]

FR12_SECTIONS = ("Вывод", "Ключевые цифры", "Анализ", "Где в модели", "Оговорки")

ANSWER_SYSTEM_PROMPT = (
    "Ты — кредитный аналитик. Отвечай деловым русским языком. Используй только числа из результатов "
    "инструментов и из запроса пользователя, ничего не досчитывай сам. Структура ответа — разделы с заголовками: "
    "«Вывод» (1–3 предложения), «Ключевые цифры» (таблица), «Анализ», «Где в модели», «Оговорки». "
    "Пустые разделы опускай. Не упоминай названия инструментов, коды ошибок и служебные идентификаторы."
)
