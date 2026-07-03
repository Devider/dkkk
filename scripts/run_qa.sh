#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://localhost:8080"
U="test"
TRACE_ID="550e8400-e29b-41d4-a716-446655440000"
CLIENT_ID="CI12345678"
SESSION_ID="550e8400-e29b-41d4-a716-446655440001"
REQUEST_TIME="2026-06-27T12:00:00Z"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

HEADERS=(
  -H "x-trace-id: $TRACE_ID"
  -H "x-client-id: $CLIENT_ID"
  -H "x-session-id: $SESSION_ID"
  -H "x-user-id: $U"
  -H "x-request-time: $REQUEST_TIME"
)

echo "=== Uploading model.xlsx ==="
curl -s -X POST "$BASE_URL/api/v1/upload" \
  -F "file=@models/model.xlsx" \
  "${HEADERS[@]}" > /dev/null
echo "OK"
echo

Q1='Проанализируй model.xlsx со значениями метанола 2025 (450, 500) с шагом 5 и инфляции USD CPI 2025 (0,1 , 0,2) с шагом 0,1. Покажи изменения debt/ebitda, net debt/ebitda (ltm) и icr corr (ltm) 2025.'

Q2='Скажи, при каких значениях цены метанола и роста потребительских цен США в модели model.xlsx значение ebitda 2026 будет 1000?'

Q3='В модели model.xlsx сделай прирост цены метанола с 2025 года на 100 каждый год и инфляция потребительских цен США на 0.1 каждый год. Покажи новые и старые значения ebitda в сводной таблице.'

for i in 1 2 3; do
  qvar="Q$i"
  question="${!qvar}"
  zipfile="$TMPDIR/q$i.zip"
  SESSION_ID="550e8400-e29b-41d4-a716-44665544000$i"

  echo "=== Q$i ==="
  echo "$question"
  echo

  curl -s -X POST "$BASE_URL/api/v1/invoke-agent" \
    -H "Content-Type: application/json" \
    -d "{\"message\": $(printf '%s' "$question" | jq -Rs .)}" \
    -H "x-trace-id: $TRACE_ID" \
    -H "x-client-id: $CLIENT_ID" \
    -H "x-session-id: $SESSION_ID" \
    -H "x-user-id: $U" \
    -H "x-request-time: $REQUEST_TIME" \
    --output "$zipfile"

  answer=$(python3 -c "
import sys, zipfile
z = zipfile.ZipFile('$zipfile')
print(z.read('txt_response.txt').decode())
")

  echo "$answer"
  echo
done
