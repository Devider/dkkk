#!/usr/bin/env python3
"""Парсит логи сервера и извлекает метрики LLM-вызовов.

Ищет JSON-строки в поле `message` логов loguru и парсит их.

Пример лога:
{"levelName": "INFO", "moduleName": "aigw_service.api.v1.subagents.analyzer", "funcName": "anlyze_query",
 "message": "{\"node\": \"anlyze_query\", \"elapsed_s\": 15.48, \"token_usage\": {...}}"}

Usage:
    python scripts/parse_llm_logs.py server.log
    python scripts/parse_llm_logs.py server.log --json
    python scripts/parse_llm_logs.py server.log --summary
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Парсит одну строку лога loguru."""
    line = line.strip()
    if not line:
        return None

    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None

    message = entry.get("message", "")
    if not message:
        return None

    # Пытаемся распарсить JSON из поля message
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return None

    # Проверяем что это наш LLM-лог (есть поле node или node+elapsed_s)
    if "node" in data or "elapsed_s" in data:
        return {
            "timestamp": entry.get("asctime"),
            "node": data.get("node", ""),
            "elapsed_s": data.get("elapsed_s"),
            "token_usage": data.get("token_usage", {}),
            "extra": {k: v for k, v in data.items() if k not in ("node", "elapsed_s", "token_usage")},
        }

    return None


def parse_log_file(log_path: str) -> list[dict[str, Any]]:
    """Читает файл логов и возвращает список LLM-вызовов."""
    results = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            parsed = parse_log_line(line)
            if parsed:
                results.append(parsed)
    return results


def print_summary(data: list[dict[str, Any]]) -> None:
    """Выводит сводную статистику."""
    if not data:
        print("Нет LLM-вызовов в логах")
        return

    print(f"Всего LLM-вызовов: {len(data)}\n")

    # Группировка по нод
    nodes: dict[str, list] = {}
    for d in data:
        node = d["node"]
        nodes.setdefault(node, []).append(d)

    for node, entries in sorted(nodes.items()):
        elapsed = [e["elapsed_s"] for e in entries if e["elapsed_s"] is not None]
        if not elapsed:
            continue

        total_tokens = [
            e["token_usage"].get("total_tokens", 0)
            for e in entries
            if e["token_usage"]
        ]

        print(f"  {node}:")
        print(f"    Вызовов:      {len(entries)}")
        print(f"    Время (с):     avg={sum(elapsed)/len(elapsed):.2f} "
              f"min={min(elapsed):.2f} max={max(elapsed):.2f}")
        if total_tokens:
            print(f"    Токены:       total_sum={sum(total_tokens)} "
                  f"avg={sum(total_tokens)/len(total_tokens):.0f}")

        # Распределение времени
        if elapsed:
            fast = len([t for t in elapsed if t < 5])
            mid = len([t for t in elapsed if 5 <= t < 15])
            slow = len([t for t in elapsed if 15 <= t < 30])
            very_slow = len([t for t in elapsed if t >= 30])
            print(f"    Распределение: <5s={fast} 5-15s={mid} 15-30s={slow} >=30s={very_slow}")

        print()


def print_json(data: list[dict[str, Any]]) -> None:
    """Выводит все данные в формате JSON."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def print_table(data: list[dict[str, Any]]) -> None:
    """Выводит таблицу с деталями."""
    if not data:
        print("Нет LLM-вызовов в логах")
        return

    print(f"{'No':>4s} {'Node':<20s} {'Elapsed(s)':>12s} {'Tokens':>12s} {'Prompt':>10s} {'Comp':>10s}")
    print("  " + "─" * 70)
    for i, d in enumerate(data, 1):
        node = d["node"]
        elapsed = d["elapsed_s"] or "?"
        tu = d["token_usage"] or {}
        total = tu.get("total_tokens", "?")
        prompt = tu.get("prompt_tokens", "?")
        comp = tu.get("completion_tokens", "?")

        print(f"{i:>4d} {node:<20s} {elapsed:>12.2f} {total:>12} {prompt:>10} {comp:>10}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Парсит логи сервера и извлекает метрики LLM")
    parser.add_argument("logfile", type=str, help="Путь к файлу логов")
    parser.add_argument("--json", action="store_true", help="Вывести в формате JSON")
    parser.add_argument("--summary", action="store_true", help="Показать сводку")
    parser.add_argument("--table", action="store_true", help="Показать таблицу")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_path = Path(args.logfile)

    if not log_path.exists():
        print(f"Файл не найден: {log_path}", file=sys.stderr)
        return 1

    data = parse_log_file(args.logfile)

    if not data:
        print("Нет LLM-вызовов в логах", file=sys.stderr)
        return 1

    if args.json:
        print_json(data)
    elif args.summary:
        print_summary(data)
    else:
        print_table(data)
        print(f"\nВсего вызовов: {len(data)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
