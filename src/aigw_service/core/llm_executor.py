import asyncio
import uuid
from contextvars import ContextVar
from enum import Enum, auto
from random import random

from langchain_gigachat import GigaChat

from aigw_service.config import APP_CONFIG




class ActionType(Enum):
    """Типы действий для отслеживания истории вызовов."""

    AINVOKE = auto()
    INVOKE = auto()
    BIND_TOOLS = auto()
    CALL_TOOL = auto()  # Добавлен для более точного отслеживания

class Agent(GigaChat):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        preview_kwargs = kwargs.copy()
        preview_kwargs["model"] = f"{preview_kwargs['model']}-preview"
        self._model_preview = GigaChat(*args, **preview_kwargs)

        # Инициализируем объект llm для оценки схожести
        self._llm_judge = GigaChat(*args, **kwargs)

        # Создаем задачу для обработки фоновых проверок
        self._check_queue = asyncio.Queue()
        self._background_task = None
        self._logger = None

    async def start_background_worker(self, logger):
        """Запуск фоновой задачи при старте приложения"""
        self._background_task = asyncio.create_task(self._process_background_checks())
        self._logger = logger
        print("✅ Агент и фоновая задача запущены")

    async def stop_background_worker(self):
        """Остановка фоновой задачи при выключении приложения"""
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                print("🛑 Фоновая задача остановлена")

    async def ainvoke(self, *args, **kwargs):

        result = await super().ainvoke(*args, **kwargs)

        # Добавляем задачу в очередь для фоновой проверки
        def print_callback(result1, result2, similarity):
            comparison_message = (
                f"=== СРАВНЕНИЕ РЕЗУЛЬТАТОВ МОДЕЛЕЙ ===\n"
                f"РЕЗУЛЬТАТ ОСНОВНОЙ МОДЕЛИ: {result1}\n"
                f"---\n"
                f"РЕЗУЛЬТАТ PREVIEW МОДЕЛИ: {result2}\n"
                f"---\n"
                f"АНАЛИЗ РАЗЛИЧИЙ: {similarity}\n"
                f"====================================="
            )
            self._logger.info(comparison_message)

        if APP_CONFIG.app.preview_model_check and random() <= APP_CONFIG.app.preview_model_check_percent:
            self._check_queue.put_nowait((result, args, kwargs, ActionType.AINVOKE, print_callback))

        return result

    def invoke(self, *args, **kwargs):

        result = super().invoke(*args, **kwargs)

        # Добавляем задачу в очередь для фоновой проверки
        def print_callback(result1, result2, similarity):
            comparison_message = (
                f"=== СРАВНЕНИЕ РЕЗУЛЬТАТОВ МОДЕЛЕЙ ===\n"
                f"РЕЗУЛЬТАТ ОСНОВНОЙ МОДЕЛИ: {result1}\n"
                f"---\n"
                f"РЕЗУЛЬТАТ PREVIEW МОДЕЛИ: {result2}\n"
                f"---\n"
                f"АНАЛИЗ РАЗЛИЧИЙ: {similarity}\n"
                f"====================================="
            )
            self._logger.info(comparison_message)

        if APP_CONFIG.app.preview_model_check and random() <= APP_CONFIG.app.preview_model_check_percent:
            self._check_queue.put_nowait((result, args, kwargs, ActionType.INVOKE, print_callback))

        return result

    def bind_tools(self, *args, **kwargs):
        result = super().bind_tools(*args, **kwargs)

        # Добавляем задачу в очередь для фоновой проверки
        def print_callback(result1, result2, similarity):
            comparison_message = (
                f"=== СРАВНЕНИЕ РЕЗУЛЬТАТОВ МОДЕЛЕЙ ===\n"
                f"РЕЗУЛЬТАТ ОСНОВНОЙ МОДЕЛИ: {result1}\n"
                f"---\n"
                f"РЕЗУЛЬТАТ PREVIEW МОДЕЛИ: {result2}\n"
                f"---\n"
                f"АНАЛИЗ РАЗЛИЧИЙ: {similarity}\n"
                f"====================================="
            )
            self._logger.info(comparison_message)

        if APP_CONFIG.app.preview_model_check and random() <= APP_CONFIG.app.preview_model_check_percent:
            self._check_queue.put_nowait((result, args, kwargs, ActionType.BIND_TOOLS, print_callback))

        return result

    async def _process_background_checks(self):
        while True:
            result_main, args, kwargs, action_type, print_callback = await self._check_queue.get()
            if action_type == ActionType.AINVOKE:
                result_preview = await self._model_preview.ainvoke(*args, **kwargs)
            elif action_type == ActionType.INVOKE:
                result_preview = self._model_preview.invoke(*args, **kwargs)
            elif action_type == ActionType.BIND_TOOLS:
                result_preview = self._model_preview.bind_tools(*args, **kwargs)
            else:
                raise ValueError(f"Unknown action type: {action_type}")

            similarity = await self._compare_results_with_llm(result_main, result_preview)

            if print_callback:
                print_callback(result_main, result_preview, similarity)

            self._check_queue.task_done()

    async def _compare_results_with_llm(self, result1, result2):
        prompt = (
            f"Сравни два результата и оцени их схожесть по шкале от 0 до 100. "
            f"Результат 1: {result1}\nРезультат 2: {result2}\n"
            f"Верни анализ различий и итоговый процент схожести в формате: "
            f"<анализ различий> | Итоговый процент схожести: <число>%"
        )
        response = await self._llm_judge.ainvoke(prompt)
        try:
            return response
        except ValueError:
            return "Ошибка оценки"
