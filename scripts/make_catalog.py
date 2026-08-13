#!/usr/bin/env python3
"""Build the candidate catalog (available input/output names) from models/model.xlsx.

Only rows that actually carry values are included (section headers and dead
template rows are dropped — the AGENTS.md planned fix), deduplicated by name,
split into two sections (Inputs / Outputs) so the model can be told to draw
input_names only from Inputs and output names only from Outputs.

Usage:
    python scripts/make_catalog.py [--model models/model.xlsx] [-o test_output/catalog.txt]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bench_resolution import build_mappings

INSTRUCTIONS = """Ниже — ПОЛНЫЙ список доступных параметров Excel-модели.
Используй ТОЛЬКО названия из этого списка, КАК ЕСТЬ (точно: скобки, дефисы, регистр).
НЕ добавляй год к названию, НЕ перефразируй, НЕ сокращай.

Правила:
- для входных параметров (input_names) бери ТОЛЬКО названия из секции «ВХОДНЫЕ ПАРАМЕТРЫ (Inputs)»;
- для выходных показателей (output_names / output_name) бери ТОЛЬКО из секции «ВЫХОДНЫЕ ПОКАЗАТЕЛИ (Outputs)»;
- если пользователь не упомянул «(LTM)» — выбирай базовый показатель без «(LTM)», а не LTM-вариант;
- если есть несколько похожих названий — выбирай то, что точнее всего соответствует запросу пользователя.
"""

HEADER = """СПИСОК ДОСТУПНЫХ ПАРАМЕТРОВ МОДЕЛИ
{}

ВХОДНЫЕ ПАРАМЕТРЫ (Inputs):
{inputs}

ВЫХОДНЫЕ ПОКАЗАТЕЛИ (Outputs):
{outputs}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build candidate catalog from the Excel model")
    parser.add_argument("--model", default="models/model.xlsx")
    parser.add_argument("-o", "--out", default="test_output/catalog.txt")
    args = parser.parse_args()

    input_mapping, output_mapping = build_mappings(args.model)

    def names(mapping: dict, key: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for info in mapping[key].values():
            if not info.get("values"):
                continue  # section headers / dead template rows
            name = info["original"].strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return sorted(out, key=str.lower)

    inputs, outputs = names(input_mapping, "row_mapping"), names(output_mapping, "output_mapping")
    text = HEADER.format(
        INSTRUCTIONS,
        inputs="\n".join(inputs),
        outputs="\n".join(outputs),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"catalog inputs: {len(inputs)}, outputs: {len(outputs)}, total: {len(inputs) + len(outputs)}")
    print(f"wrote -> {out_path}  ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
