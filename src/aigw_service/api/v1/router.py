import tempfile
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiofiles
import gigachat.context as gc_ctx
import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langfuse.langchain import CallbackHandler

from aigw_service.api.v1.main_graph import AgentGraph
from aigw_service.api.v1.schemas_file import CopilotAgentRequest, FailedDependencyResponse, FileLoaderResponse
from aigw_service.api.v1.utils import common_headers
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


# =====================================================================================================================
# ВЫЗОВ АГЕНТА
# =====================================================================================================================
@lru_cache(maxsize=1)
def get_agent():
    try:
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
    headers: dict = Depends(common_headers),
    agent: AgentGraph = Depends(get_agent),
    cb_handler: Optional[CallbackHandler] = Depends(get_cb_handler()),
) -> StreamingResponse:
    _setup_request_headers(headers=headers)
    agent.logger = logger
    user_id = headers.get("x-user-id")
    x_client_id = headers.get("x-client-id")
    session_id = headers.get("x-session-id") or str(uuid4())
    # Composite key: MemorySaver partitions purely by thread_id, with no notion of user_id, so
    # two different users sharing (or both lacking) a session id would otherwise see each
    # other's conversation history once the checkpointer is attached.
    thread_id = f"{user_id}:{session_id}"

    try:
        logger.info(
            "Incoming user request: user_id={}, session={}, message={}...",
            user_id,
            thread_id,
            request.message[:200] if request.message else "",
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
                "endpoint": "/invoke-agent",
            },
        }
        if cb_handler is not None:
            config["callbacks"] = [cb_handler]
        else:
            logger.warning("cb_handler is None - LAngfuse tracing is disabled or unavailable, skipping callbacks")
        logger.info("config before entering process message: {}", config)
        result = await agent.graph.ainvoke(
            {
                "messages": [HumanMessage(content=request.message)],
                "thread_id": thread_id,
                "user_id": user_id,
            },
            config=config,
        )

        logger.info("Invoking agent with thread_id={}, user_id={}", thread_id, user_id)
        msg = result["messages"][-1]
        result_content = msg.content if hasattr(msg, "content") else str(msg)
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
