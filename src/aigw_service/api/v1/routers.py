import tempfile
from functools import cache
from pathlib import Path

import aiofiles
import gigachat.context as gc_ctx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError

from .schemas import CopilotAgentRequest, FailedDependencyResponse, FileLoaderResponse
from .services import Agent
from .utils import common_headers

router = APIRouter()
logger = APP_CTX.get_logger()


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

    Args:
        file: Загружаемый файл
        headers: заголовки
    Returns:
        common
    """

    logger.info(f"HEADERS: {headers}")

    logger.info("Graph agent endpoint called")

    # Сквозной проброс заголовка "x-trace-id" в GigaChat
    x_trace_id = headers.get("x-trace-id")
    gc_ctx.trace_id_cvar.set(x_trace_id)

    excel_extentions = {"xlsx", "xls"}

    try:
        save_dir = Path(tempfile.gettempdir())
        file_extension = file.filename.split(".")[-1] if "." in file.filename else ""

        if file_extension not in excel_extentions:
            raise HTTPException(status_code=400, detail="Файл должен быть с расширением Excel")

        filename = f"user_file_{str(headers['x-user-id'])}.{file_extension}"
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

        logger.info(f"File saved to {file_location}, store: {store_items}")

        return FileLoaderResponse(content="Файл был успешно сохранен.", filename=str(filename), save_dir=str(save_dir))
    except Exception as e:
        # pylint: disable=no-member
        logger.error(f"Request failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content=FailedDependencyResponse(error_description=str(e)).model_dump(),
        )


@cache
def get_agent():
    try:
        logger = APP_CTX.get_logger()
    except Exception as e:
        logger.error(f"Failed to import Agent: {e}")
        raise

    try:
        agent = Agent(logger=logger)
        return agent
    except Exception as e:
        logger.error(f"Failed to create Agent instance: {e}")
        raise


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
                "x-client-id": "CI00163870",
                "message": "Проведи анализ модели и подготовь отчёт",
            }
        ],
    },
)
async def invoke_agent(
    request: CopilotAgentRequest,
    headers: dict = Depends(common_headers),
    # graph: CompiledStateGraph = Depends(get_agent)
    agent: Agent = Depends(get_agent),
) -> StreamingResponse:
    logger = APP_CTX.get_logger()
    agent.logger = logger
    thread_id = headers.get("x-session-id")
    user_id = headers.get("x-user-id")
    x_trace_id = headers.get("x-trace-id")
    gc_ctx.trace_id_cvar.set(x_trace_id)

    try:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
            }
        }

        logger.info(f"config before entering process message: {config}")
        result = await agent.process_message(
            {"messages": [{"role": "user", "content": request.message}]}, config=config
        )

        logger.info(f"Invoking agent with thread_id={thread_id}, user_id={user_id} ")
        msg = result["messages"][-1]
        result_content = msg.content if hasattr(msg, "content") else str(msg)
        logger.info(f"Content text: {result_content}")

        total_token_usage = result.get("total_token_usage", {})
        llm_call_count = result.get("llm_call_count", 0)

        return JSONResponse(
            content={
                "content": result_content,
                "token_usage": total_token_usage,
                "llm_call_count": llm_call_count,
            }
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
        logger.error(f"Agent invocation failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content=FailedDependencyResponse(error_description=str(e)).model_dump(),
        )
