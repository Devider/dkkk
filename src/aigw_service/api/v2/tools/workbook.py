"""Инструменты deep agent'а для чтения структуры загруженной Excel-модели."""

import asyncio

import openpyxl
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from aigw_service.api.v2.tools.storage import FILE_NOT_FOUND_MESSAGE, resolve_uploaded_file_path
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

SHEETS_READ_ERROR_MESSAGE = "Не удалось прочитать список листов файла модели."


def _read_sheet_names(file_path: str) -> list[str]:
    # read_only не загружает ячейки — достаточно для имён листов и быстро даже на больших моделях.
    wb = openpyxl.load_workbook(file_path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


@tool
async def list_excel_sheets(runtime: ToolRuntime) -> str:
    """Открывает загруженный Excel-файл модели и возвращает список названий его листов.

    Вызывай, когда пользователь спрашивает, какие листы есть в его файле модели.
    """
    file_path = await resolve_uploaded_file_path(runtime, "list_excel_sheets")
    if file_path is None:
        return FILE_NOT_FOUND_MESSAGE

    try:
        sheet_names = await asyncio.to_thread(_read_sheet_names, file_path)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.opt(exception=True).error("list_excel_sheets: failed to read {}: {}", file_path, str(e))
        return SHEETS_READ_ERROR_MESSAGE

    if not sheet_names:
        return "В файле модели нет листов."
    return "Листы файла модели:\n" + "\n".join(f"- {name}" for name in sheet_names)
