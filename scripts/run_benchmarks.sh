#!/usr/bin/env bash
# Запускает все три метода резолвинга имён (как есть, с порогами по умолчанию),
# затем сравнивает JSON-результаты и выводит сводную статистику.
#
# Usage:
#   scripts/run_benchmarks.sh                # полный прогон: jaccard + embeddings + hybrid
#   scripts/run_benchmarks.sh --subset 20    # любые аргументы пробрасываются в bench_resolution.py
#   scripts/run_benchmarks.sh --no-hybrid    # пропустить hybrid (медленный, требует LLM)
#
# Метод, упавший с ошибкой (например, GigaChat недоступен), пропускается;
# сравнение строится по успешно отработавшим JSON.
set -uo pipefail

cd "$(dirname "$0")/.."

if [ -x venv/bin/python ]; then
    PY=venv/bin/python
else
    PY=python3
fi

OUT=test_output/bench
mkdir -p "$OUT"

args=()
no_hybrid=0
for a in "$@"; do
    if [ "$a" = "--no-hybrid" ]; then
        no_hybrid=1
    else
        args+=("$a")
    fi
done

ok=()

run() {
    local method="$1"
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  METHOD: $method"
    echo "════════════════════════════════════════════════════════════"
    if "$PY" scripts/bench_resolution.py --method "$method" --out "$OUT/bench_$method.json" "${args[@]}"; then
        ok+=("$method")
    else
        echo "!! $method завершился с ошибкой — пропущен в сравнении"
    fi
}

run jaccard
run embeddings
if [ "$no_hybrid" -eq 0 ]; then
    run hybrid
else
    echo ""
    echo "(hybrid пропущен — --no-hybrid)"
fi

if [ "${#ok[@]}" -eq 0 ]; then
    echo ""
    echo "Нет успешных прогонов — нечего сравнивать."
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  СРАВНЕНИЕ МЕТОДОВ"
echo "════════════════════════════════════════════════════════════"
cmds=()
for m in "${ok[@]}"; do
    cmds+=("$OUT/bench_$m.json")
done
"$PY" scripts/bench_resolution.py --compare "${cmds[@]}"