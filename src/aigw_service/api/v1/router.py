import tempfile
import time
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiofiles
import gigachat.context as gc_ctx
import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import HumanMessage
from langfuse.langchain import CallbackHandler

from aigw_service.api.v1.main_graph import AgentGraph
from aigw_service.api.v1.schemas_file import CopilotAgentRequest, FailedDependencyResponse, FileLoaderResponse
from aigw_service.api.v1.utils import common_headers
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.stream.observe import StreamStats
from aigw_service.api.v2.stream.sse import SSE_HEADERS, one_shot, sse_body
from aigw_service.api.v2.turn.disconnect import disconnect_watch
from aigw_service.api.v2.turn.lock import TURN_BUSY_MESSAGE, TurnBusyError
from aigw_service.api.v2.turn.registry import ModelRegistry
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError

router = APIRouter()
logger = APP_CTX.get_logger()


def _setup_request_headers(headers: dict) -> None:
    """Сквозной проброс заголовка x-trace-id в GigaChat."""
    x_trace_id = headers.get("x-trace-id")
    gc_ctx.trace_id_cvar.set(x_trace_id)


# =====================================================================================================================
# ЗАГРУЗКА ФАЙЛА
# =====================================================================================================================
@router.post(
    "/upload",
    status_code=status.HTTP_200_OK,
    response_model=FileLoaderResponse,
    response_description="OK",
    summary="Загрузка файла на ПОД AI GW",
    description=(
        "Эндпоинт для загрузки Excel файла для дальнейшей обработки агентом. Файл сохраняется во временную папку, далее будет добавлено сохранение в базу."
    ),
    responses={
        status.HTTP_424_FAILED_DEPENDENCY: {
            "description": "Failed Dependency Response",
            "model": FailedDependencyResponse,
        },
    },
    openapi_extra={
        "x-AI-ready": True,
    },
)
async def upload_file(
    # pylint: disable=C0103,W0613,R0914,R0917
    file: UploadFile = File(...),
    headers: dict = Depends(common_headers),
):
    """
    Загрузка файла с использованием multipart/form-data.
    """

    _setup_request_headers(headers=headers)
    logger.info("HEADERS: {}", headers)
    logger.info("Graph agent endpoint called")

    excel_extentions = {"xlsx", "xls"}

    try:
        save_dir = Path(tempfile.gettempdir())
        file_extension = file.filename.split(".")[-1] if "." in file.filename else ""

        if file_extension not in excel_extentions:
            raise HTTPException(status_code=400, detail="Файл должен быть с расширением Excel")

        filename = f"user_file_{headers['x-user-id']!s}.{file_extension}"
        file_location = save_dir / filename

        user_id = headers["x-user-id"]
        namespace = ("memories", user_id)
        key = user_id

        store = APP_CTX.agent_memory.store

        async with aiofiles.open(file_location, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        await store.aput(namespace, key, {"filename": filename})
        if APP_CONFIG.agent.engine == "v2":
            await _register_model_v2(store, namespace, key, filename, file_location, file.filename)
        store_items = await store.aget(namespace, key)

        logger.info("File saved to {}, store: {}", file_location, store_items)

        return FileLoaderResponse(content="Файл был успешно сохранен.", filename=str(filename), save_dir=str(save_dir))
    except Exception as e:
        # pylint: disable=no-member
        logger.opt(exception=True).error("Upload failed: {}", str(e))
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content=FailedDependencyResponse(error_description=str(e)).model_dump(),
        )


async def _register_model_v2(
    store, namespace: tuple, key: str, filename: str, file_location: Path, original_name: str | None
) -> None:
    """Agent v2 (FR-1): index and card go to the model registry now, so turns never open the workbook.

    A failure here does not fail the upload: TurnContext builds the index on the first turn instead.
    """
    try:
        digest = await ModelRegistry(store).register(file_location, file_name=original_name or filename)
        await store.aput(namespace, key, {"filename": filename, "file_hash": digest})
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.opt(exception=True).warning(
            "Model registration failed, the first turn will build the index: {}", str(e)
        )


# =====================================================================================================================
# ВЫЗОВ АГЕНТА
# =====================================================================================================================
@lru_cache(maxsize=1)
def get_agent():
    """Agent v1 or v2 by AGENT_ENGINE (FR-20): switch and roll back with the variable and a restart."""
    try:
        if APP_CONFIG.agent.engine == "v2":
            return AgentV2()
        agent = AgentGraph()
        return agent
    except Exception as e:
        logger.error("Failed to create Agent instance: {}", e)
        raise


@lru_cache(maxsize=1)
def get_cb_handler():
    def get_or_create_cb_handler():
        if APP_CTX.tracing is None:
            logger.warning("Tracing is None - Langfuse not initialized, callbach will be skipped")
            return None
        if hasattr(APP_CTX.tracing, "callback_handler"):
            return APP_CTX.tracing.callback_handler
        return None

    return get_or_create_cb_handler


STREAM_PATH = "/invoke-agent/stream"
STREAM_TAG = "stream=true"
"""AR-Q18 (S6.5 design D5): the Langfuse tag of a turn started through ``STREAM_PATH``, for both engines."""


def _session_ids(headers: dict) -> tuple[str, str]:
    """``session_id`` (from the header or a new one) and ``thread_id``."""
    user_id = headers.get("x-user-id")
    session_id = headers.get("x-session-id") or str(uuid4())
    # Composite key: MemorySaver partitions purely by thread_id, with no notion of user_id, so
    # two different users sharing (or both lacking) a session id would otherwise see each
    # other's conversation history once the checkpointer is attached.
    return session_id, f"{user_id}:{session_id}"


def _turn_setup(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    message: str,
    headers: dict,
    agent: AgentGraph | AgentV2,
    cb_handler: Optional[CallbackHandler],
    session_id: str,
    endpoint: str,
) -> tuple[dict, RequestContext | None]:
    """``config`` of the turn and, for agent v2, its ``RequestContext``; shared by both invoke endpoints."""
    user_id = headers.get("x-user-id")
    x_client_id = headers.get("x-client-id")
    thread_id = f"{user_id}:{session_id}"
    logger.info(
        "Incoming user request: user_id={}, session={}, message={}...",
        user_id,
        thread_id,
        message[:200] if message else "",
    )

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
        },
        "metadata": {
            "lang_fuse_user_id": user_id,
            "client_id": x_client_id,
            "langfuse_session_id": thread_id,
            "endpoint": endpoint,
            # AR-Q13 (S4.3 D2): keys the Langfuse handler reads; the old ones above stay for dashboards
            "langfuse_user_id": user_id,
            "langfuse_tags": [f"agent_engine={'v2' if isinstance(agent, AgentV2) else 'v1'}"],
        },
    }
    if endpoint == STREAM_PATH:
        config["metadata"]["langfuse_tags"].append(STREAM_TAG)
    if cb_handler is not None:
        config["callbacks"] = [cb_handler]
    else:
        logger.warning("cb_handler is None - LAngfuse tracing is disabled or unavailable, skipping callbacks")
    logger.info("config before entering process message: {}", config)
    if not isinstance(agent, AgentV2):
        return config, None
    config["metadata"]["agent_engine"] = "v2"
    context = RequestContext(
        user_id=user_id,
        session_id=session_id,
        trace_id=headers.get("x-trace-id") or "",
        client_id=x_client_id or "",
    )
    return config, context


async def _v1_answer(agent: AgentGraph, message: str, headers: dict, thread_id: str, config: dict) -> str:
    """One turn of the v1 graph; the text of its last message."""
    user_id = headers.get("x-user-id")
    result = await agent.graph.ainvoke(
        {
            "messages": [HumanMessage(content=message)],
            "thread_id": thread_id,
            "user_id": user_id,
        },
        config=config,
    )

    logger.info("Invoking agent with thread_id={}, user_id={}", thread_id, user_id)
    msg = result["messages"][-1]
    return msg.content if hasattr(msg, "content") else str(msg)


@router.post(
    "/invoke-agent",
    status_code=status.HTTP_200_OK,
    response_description="Успешный ответ от агента в виде ZIP-архива",
    summary="Вызов ИИ-агента Copilot'a для обработки запроса пользователя",
    description=(
        "Эндпоинт для вызова агента, который обрабатывает сообщение пользователя, "
        "генерирует текстовый ответ и пустой Excel-файл, упаковывает их в ZIP и возвращает ответ."
    ),
    responses={
        status.HTTP_424_FAILED_DEPENDENCY: {
            "description": "Ошибка выполнения агента",
            "model": FailedDependencyResponse,
        },
    },
    openapi_extra={
        "x-AI-ready": True,
        "x-few-shot-examples": [
            {
                "request": "Проанализируй cashflow модель по загруженному файлу",
                "x-trace-id": "e037c70a-30d7-4c47-b25e-ce18c9c39f15",
                "x-request-time": "2025-04-08T11:31:45.748539+03:00",
                "x-client-id": "CI12345678",
                "message": "Проведи анализ модели и подготовь отчёт",
            }
        ],
    },
)
async def invoke_agent(
    request: CopilotAgentRequest,
    http_request: Request,
    headers: dict = Depends(common_headers),
    agent: AgentGraph | AgentV2 = Depends(get_agent),
    cb_handler: Optional[CallbackHandler] = Depends(get_cb_handler()),
) -> StreamingResponse:
    _setup_request_headers(headers=headers)
    agent.logger = logger
    session_id, thread_id = _session_ids(headers)

    try:
        config, context = _turn_setup(request.message, headers, agent, cb_handler, session_id, "/invoke-agent")
        if context is not None:
            async with disconnect_watch(http_request, context.cancel):  # AR-Q12: stop after the current call
                result_content = await agent.answer(request.message, context, config)
        else:
            result_content = await _v1_answer(agent, request.message, headers, thread_id, config)
        logger.info("Content text: {}", result_content)

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("txt_response.txt", result_content)

            excel_buffer = BytesIO()
            pd.DataFrame().to_excel(excel_buffer, index=False)
            zip_file.writestr("generated/agent_output.xlsx", excel_buffer.getvalue())

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=agent_response_{session_id}.zip"},
        )

    except TurnBusyError:
        logger.warning("Turn of {} is still running, request rejected with 409", thread_id)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=FailedDependencyResponse(error_description=TURN_BUSY_MESSAGE).model_dump(),
        )
    except StopEventError as e:
        logger.error(
            "StopEvent: %s (url=%s, timestamp=%s)",
            e.user_message,
            e.url,
            e.timestamp,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=FailedDependencyResponse(error_description=e.user_message).model_dump(),
        )
    except Exception as e:
        logger.opt(exception=True).error("Agent invocation failed: {}", str(e))
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content=FailedDependencyResponse(error_description=str(e)).model_dump(),
        )


# =====================================================================================================================
# ПОТОКОВЫЙ ВЫЗОВ АГЕНТА (S6.3, FR-21, ADR-13)
# =====================================================================================================================
STREAM_DESCRIPTION = (
    "Тот же ход агента, что у /invoke-agent, доставленный событиями SSE (text/event-stream). Кадр: "
    "`event: <тип>`, `id: <seq>`, `data: <JSON>`; общие поля data — v, seq, ts, trace_id. Типы: turn.started, phase, "
    "plan, tool.started, tool.finished, summary, answer, error. Поток заканчивается ровно одним событием answer или "
    "error. Пока событий нет, каждые AGENT_STREAM_HEARTBEAT с (по умолчанию 15) приходит комментарий `: ping`. "
    "Ошибки до старта хода (422, 409) — обычные HTTP-ответы. "
    "При AGENT_ENGINE=v1 поток упрощённый: turn.started, затем answer или error."
)


@router.post(
    STREAM_PATH,
    status_code=status.HTTP_200_OK,
    response_class=StreamingResponse,
    summary="Потоковый вызов ИИ-агента Copilot'a: ход агента событиями SSE",
    description=STREAM_DESCRIPTION,
    responses={
        status.HTTP_200_OK: {
            "description": "Поток событий хода (SSE), последнее событие — answer или error",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        status.HTTP_409_CONFLICT: {
            "description": "Идёт предыдущий ход этой сессии",
            "model": FailedDependencyResponse,
        },
        status.HTTP_424_FAILED_DEPENDENCY: {
            "description": "Ошибка до старта хода",
            "model": FailedDependencyResponse,
        },
    },
    openapi_extra={"x-AI-ready": True},
)
async def invoke_agent_stream(
    request: CopilotAgentRequest,
    headers: dict = Depends(common_headers),
    agent: AgentGraph | AgentV2 = Depends(get_agent),
    cb_handler: Optional[CallbackHandler] = Depends(get_cb_handler()),
) -> Response:
    """FR-21: 409 / 422 / 424 before the stream; afterwards everything, StopEvent included, is an event.

    A client that goes away is noticed by ``sse_body`` (Starlette cancels it); ``disconnect_watch`` is not used
    here because Starlette's own listener takes ``http.disconnect`` (spike S6.1, H3). The ``STREAM`` line's clock
    starts here, before the turn is set up: the time to the first event is what the client waits (AR-Q18).
    """
    started = time.perf_counter()
    _setup_request_headers(headers=headers)
    agent.logger = logger
    session_id, thread_id = _session_ids(headers)
    try:
        config, context = _turn_setup(request.message, headers, agent, cb_handler, session_id, STREAM_PATH)
        if context is not None:
            events = agent.stream(request.message, context, config)  # TurnBusyError before the first event
        else:
            events = one_shot(
                headers.get("x-trace-id") or "",
                session_id,
                lambda: _v1_answer(agent, request.message, headers, thread_id, config),
            )
    except TurnBusyError:
        logger.warning("Turn of {} is still running, stream rejected with 409", thread_id)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=FailedDependencyResponse(error_description=TURN_BUSY_MESSAGE).model_dump(),
        )
    except Exception as e:  # pylint: disable=broad-exception-caught  # same contract as /invoke-agent
        logger.opt(exception=True).error("Agent stream failed to start: {}", str(e))
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content=FailedDependencyResponse(error_description=str(e)).model_dump(),
        )
    stats = StreamStats(headers.get("x-trace-id") or "", "v2" if context is not None else "v1", started)
    body = sse_body(events, APP_CONFIG.agent.stream_heartbeat, stats)
    return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)
