#!/usr/bin/env bash
set -euo pipefail

# --- IFT endpoint ---
BASE_URL="https://pipeline-impl.apps.a13ccft0.k8s.delta.sbrf.ru/pipeline/v1/cashflow/copilot"

# --- mTLS certs (GigaChat IFT) ---
CERT_DIR="$HOME/.certs/sw_certs"
CERT="$CERT_DIR/te-tls.crt"
KEY="$CERT_DIR/te-tls.key"

# --- Default user ---
X_USER="01924968"

# --- Single session UUID (upload + message) ---
BP_UUID="574e6111-31e6-4e4d-8584-68f2245d4442"

# --- Temp dir for outputs ---
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== Uploading model.xlsx ==="
curl -s --insecure -X POST "$BASE_URL/$BP_UUID/file" \
  --cert "$CERT" --key "$KEY" \
  -H "X-USER: $X_USER" \
  -F "file=@models/model.xlsx"
echo "OK"
echo

Q1='Проанализируй model.xlsx со значениями метанола 2025 (450, 500) с шагом 5 и инфляции USD CPI 2025 (0,1 , 0,2) с шагом 0,1. Покажи изменения debt/ebitda, net debt/ebitda (ltm) и icr corr (ltm) 2025.'

Q2='Скажи, при каких значениях цены метанола и роста потребительских цен США в модели model.xlsx значение ebitda 2026 будет 1000?'

Q3='В модели model.xlsx сделай прирост цены метанола с 2025 года на 100 каждый год и инфляция потребительских цен США на 0.1 каждый год. Покажи новые и старые значения ebitda в сводной таблице.'

for i in 1 2 3; do
  qvar="Q$i"
  question="${!qvar}"
  outfile="$TMPDIR/q$i.json"

  echo "=== Q$i ==="
  echo "$question"
  echo

  curl -s --insecure -X POST "$BASE_URL/$BP_UUID/message" \
    --cert "$CERT" --key "$KEY" \
    -H "X-USER: $X_USER" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg msg "$question" '{messageRequest: $msg}')" \
    --output "$outfile"

  answer=$(python3 -c "
import sys, json
with open('$outfile') as f:
    data = json.load(f)
print(data.get('body', {}).get('messageResponse', ''))
")

  echo "$answer"
  echo
done