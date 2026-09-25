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

Q1='Привет!'

Q2='Какая сейчас погода в Москве.'

Q3='Сколько должно быть значение цены метанола, чтобы ebitda стала 1000?'

Q4='2026'

Q5='Скажи, при каких значениях цены метанола и роста потребительских цен США в модели значение ebitda 2026 будет 1000?'

Q6='Какой вывод можно сделать по последнему рассчету?'

for i in 1 2 3 4 5 6; do
  qvar="Q$i"
  question="${!qvar}"
  zipfile="$TMPDIR/q$i.zip"
  SESSION_ID="550e8400-e29b-41d4-a716-446655440031"

  echo "=== Q$i ==="
  echo "$question"
  echo

  http_code=$(curl -s -o "$zipfile" -w "%{http_code}" -X POST "$BASE_URL/api/v1/invoke-agent" \
    -H "Content-Type: application/json" \
    -d "{\"message\": $(printf '%s' "$question" | jq -Rs .)}" \
    -H "x-trace-id: $TRACE_ID" \
    -H "x-client-id: $CLIENT_ID" \
    -H "x-session-id: $SESSION_ID" \
    -H "x-user-id: $U" \
    -H "x-request-time: $REQUEST_TIME")

  if [ "$http_code" != "200" ]; then
    echo "ERROR: HTTP $http_code"
    cat "$zipfile"
    echo
    exit 1
  fi

  answer=$(python3 -c "
import sys, zipfile
z = zipfile.ZipFile('$zipfile')
print(z.read('txt_response.txt').decode())
")

  echo "$answer"
  echo
done
