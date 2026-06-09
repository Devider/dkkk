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

    if [ -n "$OLLAMA_MODEL" ]; then
        echo "> pulling model $OLLAMA_MODEL ..."
        wget -q -O- --post-data="{\"name\": \"$OLLAMA_MODEL\"}" \
            "$OLLAMA_BASE_URL/api/pull" >/dev/null 2>&1
        echo "> waiting for model $OLLAMA_MODEL to appear ..."
        for i in $(seq 1 60); do
            if wget -q -O- "$OLLAMA_BASE_URL/api/tags" | grep -q "$OLLAMA_MODEL"; then
                echo "> model $OLLAMA_MODEL ready"
                break
            fi
            echo "> retrying model check ($i)..."
            sleep 5
        done
    fi
fi

exec python3 -m aigw_service
