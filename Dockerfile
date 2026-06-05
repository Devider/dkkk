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
# Обновление системного pip
&& python3.12 -m pip install --upgrade pip \
&& /usr/bin/python3.12 -m pip install --upgrade pip

########################### Builder ############################
FROM base as builder

USER root

# Создание venv
RUN python3.12 -m venv ${POETRY_VENV} \
# Обновление pip в venv
&& ${POETRY_VENV}/bin/pip install --upgrade pip setuptools \
# Устанавливаем poetry
&& ${POETRY_VENV}/bin/pip install poetry==${POETRY_VERSION}

ENV PATH="${PATH}:${POETRY_VENV}/bin"

WORKDIR /opt/app-root

COPY poetry.lock pyproject.toml ./
COPY src/ ./src/

RUN poetry check \
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

# Копируем установленное приложение из builder stage
COPY --from=builder ${POETRY_VENV} ${POETRY_VENV}
ENV PATH="${POETRY_VENV}/bin:${PATH}"

COPY app.sh ./
RUN chmod +x app.sh

COPY waitingSecrets.sh ./
RUN chmod +x waitingSecrets.sh

ENTRYPOINT ["./app.sh"]
