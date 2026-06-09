#!/bin/sh
set -e

if [ "$MODEL_TO_USE" = "OLLAMA" ] && [ -n "$OLLAMA_BASE_URL" ]; then
    echo "> waiting for Ollama at $OLLAMA_BASE_URL ..."
    sleep 5
    for i in $(seq 1 60); do
        if wget -q -O- "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1; then
            echo "> Ollama ready"
            break
        fi
        echo "> retrying ($i)..."
        sleep 2
    done
fi

exec python3 -m aigw_service
