FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir poetry~=2.2
WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root --only main

COPY src/ ./src/
RUN poetry build --no-interaction -f wheel && \
    pip install --no-cache-dir dist/*.whl

# ---- runtime ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-calc \
    graphviz \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
WORKDIR /app
COPY src/ ./src/
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && mkdir -p /var/log/aigw

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080 \
    PYTHONPATH=/app/src

EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
