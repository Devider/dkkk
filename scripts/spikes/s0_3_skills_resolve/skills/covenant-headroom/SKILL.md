---
name: covenant-headroom
description: Запас прочности по ковенантам (DSCR, Debt/EBITDA) — «хватит ли запаса», «при какой цене пробьём DSCR 1,2». Covenant headroom, breach point.
metadata:
  include_tools: calc_headroom
---

# Запас по ковенантам

1. Сопоставь метрики и пороги через `resolve_terms`.
2. Получи базовые значения.
3. Посчитай запас инструментом `calc_headroom` для каждой ковенанты.
4. Сформулируй вывод: запас в процентах и точка пробоя.
