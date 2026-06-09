#!/bin/sh
set -e

if [ "$MODEL_TO_USE" = "OLLAMA" ] && [ -n "$OLLAMA_BASE_URL" ]; then
    until wget -q -O- "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1; do
        echo "> waiting for Ollama at $OLLAMA_BASE_URL ..."
        sleep 2
    done
    echo "> Ollama ready"
fi

exec python3 -m aigw_service
