#!/usr/bin/env bash
# Prepares a deployable copy of the project for OpenShift build.
#
# Usage:  ./scripts/prepare-deploy.sh
#
# What it does:
#   1. Copies the project to /tmp/aigw-deploy/
#   2. Uncomments sber-aigw dependency + private PyPI sources in pyproject.toml
#   3. Removes local aigw_modules/ stubs → real sber-aigw from private registry
#   4. Replaces Dockerfile with the enterprise version (no LibreOffice needed)
#   5. Adds app.sh entrypoint (as used on OpenShift)
#   6. Strips dev-only files (docker-compose, docker.env, entrypoint, .env, ...)
#   7. Cleans __pycache__, artifacts, IDE configs
#
# The resulting /tmp/aigw-deploy/ is ready for `docker build` or S2I push.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="/tmp/aigw-deploy"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Preparing deploy copy of aigw-rest-service ===${NC}"
echo "  Source: $PROJECT_DIR"
echo -e "  Target: ${YELLOW}$DEPLOY_DIR${NC}"
echo ""

# ------------------------------------------------------------------
# 1. Create clean copy
# ------------------------------------------------------------------
echo "  [1/7] Creating clean copy ..."
rm -rf "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"

rsync -a --delete \
  --exclude='.gigacode/' \
  --exclude='.vscode/' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.ruff_cache' \
  --exclude='.pytest_cache' \
  --exclude='response.zip' \
  --exclude='test-results.xml' \
  --exclude='coverage.xml' \
  --exclude='*.log' \
  --exclude='*.tar.gz' \
  --exclude='.env' \
  "$PROJECT_DIR/" "$DEPLOY_DIR/"

# ------------------------------------------------------------------
# 2. pyproject.toml — restore sber-aigw dependency and sources
# ------------------------------------------------------------------
echo "  [2/7] Restoring sber-aigw + private sources in pyproject.toml ..."

# Add sber-aigw dependency (insert after langchain-gigachat line)
if grep -q '"sber-aigw' "$DEPLOY_DIR/pyproject.toml"; then
  echo "       sber-aigw already present, skipping."
else
  sed -i '/^    "langchain-gigachat/a\    "sber-aigw (>=2.2.1,<3.0.0)",' "$DEPLOY_DIR/pyproject.toml"
  echo "       Added sber-aigw dependency."
fi

# Uncomment package sources: [[tool.poetry.source]] blocks
if grep -q '^\[\[tool\.poetry\.source\]\]' "$DEPLOY_DIR/pyproject.toml"; then
  echo "       Sources already uncommented, skipping."
else
  sed -i '/^# \[\[tool\.poetry\.source\]\]/,/^# priority = /s/^# //' "$DEPLOY_DIR/pyproject.toml"
  echo "       Uncommented private PyPI sources."
fi

# ------------------------------------------------------------------
# 3. Remove local aigw_modules stubs
# ------------------------------------------------------------------
echo "  [3/7] Removing local aigw_modules stubs ..."
if [ -d "$DEPLOY_DIR/src/aigw_modules" ]; then
  rm -rf "$DEPLOY_DIR/src/aigw_modules"
  echo "       Removed src/aigw_modules/ (real sber-aigw provides these)."
else
  echo "       Already absent, skipping."
fi

# ------------------------------------------------------------------
# 4. Replace Dockerfile with enterprise version
# ------------------------------------------------------------------
echo "  [4/7] Replacing Dockerfile with enterprise version ..."

cat > "$DEPLOY_DIR/Dockerfile" << 'DOCKERFILE'
ARG DOCKER_BASE_IMAGE

########################## Base image ###########################
FROM ${DOCKER_BASE_IMAGE} as base

USER root

RUN microdnf install -y --nodocs \
    binutils \
    python3.12 \
    tzdata \
    glibc-langpack-en \
&& microdnf clean all \
&& python3.12 -m ensurepip --upgrade

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8 \
    APP_ROOT=/opt/app-root \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_HOME=/opt/app-root/poetry \
    POETRY_VENV=/opt/app-root/poetry-venv \
    POETRY_VERSION=2.2.0 \
    HOME=/opt/app-root \
    PATH=/opt/app-root/.local/bin/:$PATH

ARG NEXUS3USER
ARG NEXUS3PASS
ARG OSCTOKENAUTH

RUN pip3 config set global.index_url "https://${OSCTOKENAUTH}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
&& pip3 config set global.extra-index-url "https://${NEXUS3USER}:${NEXUS3PASS}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple/" \
&& python3.12 -m pip install --upgrade pip \
&& /usr/bin/python3.12 -m pip install --upgrade pip

########################### Builder ############################
FROM base as builder

USER root

RUN python3.12 -m venv ${POETRY_VENV} \
&& ${POETRY_VENV}/bin/pip install --upgrade pip setuptools \
&& ${POETRY_VENV}/bin/pip install poetry==${POETRY_VERSION}

ENV PATH="${PATH}:${POETRY_VENV}/bin"

WORKDIR /opt/app-root

COPY poetry.lock pyproject.toml ./
COPY src/ ./src/

RUN poetry lock --no-interaction \
&& poetry check \
&& poetry build \
&& ${POETRY_VENV}/bin/pip install dist/*.whl

########################### Финальный образ ############################
FROM base as final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

USER root
WORKDIR /opt/app-root

COPY --from=builder ${POETRY_VENV} ${POETRY_VENV}
ENV PATH="${POETRY_VENV}/bin:${PATH}"

COPY app.sh ./
RUN chmod +x app.sh

COPY waitingSecrets.sh ./
RUN chmod +x waitingSecrets.sh

ENTRYPOINT ["./app.sh"]
DOCKERFILE

echo "       Dockerfile written (enterprise, with poetry lock + build)."

# ------------------------------------------------------------------
# 5. Add app.sh entrypoint
# ------------------------------------------------------------------
echo "  [5/7] Adding app.sh entrypoint ..."

cat > "$DEPLOY_DIR/app.sh" << 'APPSCRIPT'
# APP_SCRIPT для запуска в OpenShift
exec /opt/app-root/poetry-venv/bin/aigw-rest-service-sh
APPSCRIPT

chmod +x "$DEPLOY_DIR/app.sh"
echo "       app.sh written."

# ------------------------------------------------------------------
# 6. Remove dev-only files
# ------------------------------------------------------------------
echo "  [6/7] Stripping dev-only files ..."

rm -f "$DEPLOY_DIR/docker-compose.yml"
rm -f "$DEPLOY_DIR/docker.env"
rm -f "$DEPLOY_DIR/docker-entrypoint.sh"
rm -f "$DEPLOY_DIR/oc"
rm -f "$DEPLOY_DIR/Dockerfile.SBER"
rm -f "$DEPLOY_DIR/.env"

# Retain .dockerignore and .gitignore (useful for docker build context)
echo "       Removed: docker-compose.yml docker.env docker-entrypoint.sh oc Dockerfile.SBER"

# ------------------------------------------------------------------
# 7. Verify result
# ------------------------------------------------------------------
echo "  [7/7] Verifying deploy copy ..."

PYTHON_VERSION=$(python3 --version 2>/dev/null || echo "N/A")

echo ""
echo -e "${GREEN}=== Deploy copy ready at $DEPLOY_DIR ===${NC}"
echo ""
echo "  Contents:"
echo "    $(wc -l < "$DEPLOY_DIR/pyproject.toml") lines in pyproject.toml"
echo "    $(find "$DEPLOY_DIR/src" -name '*.py' | wc -l) Python files in src/"

if grep -q '"sber-aigw' "$DEPLOY_DIR/pyproject.toml"; then
  echo -e "    ${GREEN}sber-aigw: present${NC}"
else
  echo -e "    ${RED}sber-aigw: MISSING!${NC}"
fi

if grep -q '^\[\[tool\.poetry\.source\]\]' "$DEPLOY_DIR/pyproject.toml"; then
  echo -e "    ${GREEN}Private sources: uncommented${NC}"
else
  echo -e "    ${RED}Private sources: MISSING!${NC}"
fi

if [ ! -d "$DEPLOY_DIR/src/aigw_modules" ]; then
  echo -e "    ${GREEN}src/aigw_modules/: removed${NC}"
else
  echo -e "    ${RED}src/aigw_modules/: STILL PRESENT!${NC}"
fi

if [ ! -f "$DEPLOY_DIR/docker-compose.yml" ]; then
  echo -e "    ${GREEN}docker-compose.yml: removed${NC}"
else
  echo -e "    ${RED}docker-compose.yml: STILL PRESENT!${NC}"
fi

if grep -q 'poetry lock' "$DEPLOY_DIR/Dockerfile"; then
  echo -e "    ${GREEN}Dockerfile: uses poetry lock${NC}"
else
  echo -e "    ${RED}Dockerfile: MISSING poetry lock!${NC}"
fi

echo ""
echo "  To build:"
echo "    cd $DEPLOY_DIR"
echo "    docker build "
echo "      --build-arg DOCKER_BASE_IMAGE=<base> \\"
echo "      --build-arg NEXUS3USER=<user>        \\"
echo "      --build-arg NEXUS3PASS=<pass>        \\"
echo "      --build-arg OSCTOKENAUTH=<token>     \\"
echo "      -t aigw-rest-service:latest ."
