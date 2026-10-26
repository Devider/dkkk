FROM python:3.12-slim AS builder

# PyPI or the internal mirror; pip reads it from the environment of every RUN below
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN pip install --no-cache-dir poetry==2.3.4 poetry-plugin-export==1.10.1
WORKDIR /app

# Install exactly what poetry.lock pins (with hashes), from PIP_INDEX_URL
COPY pyproject.toml poetry.lock ./
RUN poetry export --only main -f requirements.txt -o requirements.txt \
    && pip install --no-cache-dir --require-hashes -r requirements.txt

# --no-deps: dependencies are already installed from the lock, don't re-resolve them
COPY src/ ./src/
RUN poetry build --no-interaction -f wheel && \
    pip install --no-cache-dir --no-deps dist/*.whl

# ---- runtime ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    graphviz \
    wget \
    libreoffice-core \
    libreoffice-calc \
    libreoffice-script-provider-python \
    python3-uno \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
WORKDIR /app
COPY src/ ./src/
COPY scripts/docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && mkdir -p /var/log/aigw

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080 \
    PYTHONPATH=/app/src:/usr/lib/python3/dist-packages:/usr/lib/libreoffice/program \
    URE_BOOTSTRAP=vnd.sun.star.pathname:/usr/lib/libreoffice/program/fundamentalrc

EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
