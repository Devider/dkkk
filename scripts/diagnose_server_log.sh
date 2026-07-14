#!/usr/bin/env bash
set -euo pipefail

LOG="${1:-server.log}"
[ -f "$LOG" ] || { echo "Usage: $0 <server.log>"; exit 1; }

OUTDIR="diagnosis_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Scanning $LOG -> $OUTDIR/ ==="

# ---------- 1. StopEvent ----------
echo "--- 1. StopEvent occurrences ---"
grep -c '"StopEventError\|stop_event\|Stop event' "$LOG" > "$OUTDIR/1_stop_event_count.txt" 2>&1 || true
grep '"StopEventError\|stop_event\|Stop event' "$LOG" | tail -20 | python3 -c "
import sys, json
for l in sys.stdin:
    try: print(json.dumps(json.loads(l.strip()), indent=2, ensure_ascii=False))
    except: print(l.strip())
" > "$OUTDIR/1_stop_event_samples.json" 2>/dev/null || true

# ---------- 2. 403 / ForbiddenError ----------
echo "--- 2. 403 / ForbiddenError ---"
grep -ci 'HTTP.*403\|ForbiddenError\|status_code.*403\|403 Forbidden' "$LOG" > "$OUTDIR/2_403_count.txt" 2>&1 || true
grep -i 'HTTP.*403\|ForbiddenError\|status_code.*403\|403 Forbidden' "$LOG" | tail -20 \
    > "$OUTDIR/2_403_samples.txt" 2>&1 || true

# ---------- 3. Timeouts ----------
echo "--- 3. Timeouts ---"
grep -ci 'timeout\|timed.ut' "$LOG" > "$OUTDIR/3_timeout_count.txt" 2>&1 || true
grep -i '"timed out\|ReadTimeout\|ConnectTimeout\|TimeoutError' "$LOG" | tail -20 \
    > "$OUTDIR/3_timeout_samples.txt" 2>&1 || true

# ---------- 4. HTTP 503 ----------
echo "--- 4. HTTP 503 ---"
grep -c '"status_code": 503\|503 Service Unavailable' "$LOG" > "$OUTDIR/4_503_count.txt" 2>&1 || true
grep '"status_code": 503' "$LOG" | tail -10 | python3 -c "
import sys, json
for l in sys.stdin:
    try: print(json.dumps(json.loads(l.strip()), indent=2, ensure_ascii=False))
    except: print(l.strip())
" > "$OUTDIR/4_503_samples.json" 2>/dev/null || true

# ---------- 5. HTTP status code distribution ----------
echo "--- 5. HTTP status code distribution ---"
grep -oP '"status_code": \d+' "$LOG" | sort | uniq -c | sort -rn \
    > "$OUTDIR/5_status_distribution.txt" 2>&1 || true

# ---------- 6. Request duration stats ----------
echo "--- 6. Request duration ---"
grep -oP '"duration": [\d.]+' "$LOG" | python3 -c "
import sys
vals = [float(l.split()[-1]) for l in sys.stdin if l.strip()]
if vals:
    print(f'count: {len(vals)}, min: {min(vals):.1f}s, max: {max(vals):.1f}s, avg: {sum(vals)/len(vals):.1f}s')
    print(f'median (sorted): {sorted(vals)[len(vals)//2]:.1f}s')
    top = sorted(vals, reverse=True)[:10]
    print(f'top-10 longest: {top}')
    # p95
    idx95 = int(len(vals) * 0.95)
    print(f'p95: {sorted(vals)[idx95]:.1f}s')
    # p99
    idx99 = int(len(vals) * 0.99)
    print(f'p99: {sorted(vals)[idx99]:.1f}s')
    # доля >300s
    over300 = sum(1 for v in vals if v > 300)
    print(f'over 300s: {over300}/{len(vals)} ({100*over300//len(vals)}%)')
else:
    print('no duration fields found')
" > "$OUTDIR/6_duration_stats.txt" 2>&1 || true

# ---------- 7. ERROR level logs ----------
echo "--- 7. ERROR level logs ---"
grep -c '"levelName": "ERROR"' "$LOG" > "$OUTDIR/7_error_count.txt" 2>&1 || true
grep '"levelName": "ERROR"' "$LOG" | tail -30 | python3 -c "
import sys, json
for l in sys.stdin:
    try:
        d = json.loads(l.strip())
        print(json.dumps({k: d.get(k) for k in ['levelName','asctime','moduleName','funcName','message']}, indent=2, ensure_ascii=False))
    except:
        print(l.strip())
" > "$OUTDIR/7_error_samples.json" 2>/dev/null || true

# ---------- 8. Gigachat retry logs ----------
echo "--- 8. Gigachat retry / retry logs ---"
grep -ci 'gigachat.retry\|retrying\|attempt.*retry' "$LOG" > "$OUTDIR/8_retry_count.txt" 2>&1 || true
grep -i 'gigachat.retry\|retrying\|attempt.*retry' "$LOG" | tail -20 \
    > "$OUTDIR/8_retry_samples.txt" 2>&1 || true

# ---------- 9. All 5xx errors ----------
echo "--- 9. Server 5xx errors ---"
grep -cP '"status_code": 5[0-9][0-9]' "$LOG" > "$OUTDIR/9_5xx_count.txt" 2>&1 || true
grep -P '"status_code": 5[0-9][0-9]' "$LOG" | tail -20 | python3 -c "
import sys, json
for l in sys.stdin:
    try: print(json.dumps(json.loads(l.strip()), indent=2, ensure_ascii=False))
    except: print(l.strip())
" > "$OUTDIR/9_5xx_samples.json" 2>/dev/null || true

# ---------- 10. LLM calls per model ----------
echo "--- 10. LLM model used ---"
grep -oP '"model": "[^"]+"' "$LOG" | sort | uniq -c | sort -rn \
    > "$OUTDIR/10_model_distribution.txt" 2>&1 || true

echo "=== Done. Files in $OUTDIR/ ==="
ls -la "$OUTDIR/"
