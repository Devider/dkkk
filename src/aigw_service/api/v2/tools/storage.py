"""Инструменты deep agent'а для работы с хранилищем загруженных файлов."""

import os
import tempfile

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

FILE_FOUND_MESSAGE = "Файл модели загружен и доступен для расчётов."
FILE_NOT_FOUND_MESSAGE = "Файл модели не загружен. Пользователю нужно загрузить Excel-файл модели."


async def resolve_uploaded_file_path(runtime: ToolRuntime, tool_name: str) -> str | None:
    """Возвращает путь к загруженному файлу модели или None, если его нет в store или на диске."""
    user_id = runtime.state.get("user_id")
    if not user_id:
        return None

    stored = await APP_CTX.agent_memory.store.aget(("memories", user_id), user_id)
    file_name = stored.value.get("filename") if stored and isinstance(stored.value, dict) else None
    if not file_name:
        logger.info("{}: no file in store for user_id={}", tool_name, user_id)
        return None

    file_path = os.path.join(tempfile.gettempdir(), file_name)
    if not os.path.isfile(file_path):
        logger.warning("{}: store has {} but file is missing on disk", tool_name, file_path)
        return None

    return file_path


@tool
async def check_uploaded_file(runtime: ToolRuntime) -> str:
    """Проверяет, загрузил ли пользователь Excel-файл модели и доступен ли он для расчётов.

    Вызывай, когда нужно убедиться, что файл модели есть, прежде чем отвечать пользователю.
    """
    if await resolve_uploaded_file_path(runtime, "check_uploaded_file") is None:
        return FILE_NOT_FOUND_MESSAGE
    return FILE_FOUND_MESSAGE
