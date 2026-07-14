import logging
from datetime import UTC, datetime

import httpx
import pytz
from gigachat.exceptions import AuthenticationError, ForbiddenError
from httpx import RequestError
from langchain_gigachat import GigaChat, GigaChatEmbeddings
from langgraph.store.memory import InMemoryStore

from aigw_modules.ai_agents.memory import AsyncAgentMemory
from aigw_modules.base import BaseAsyncInterface
from aigw_modules.hub_services.pangolin import AsyncPangolinClient
from aigw_service.base import Singleton
from aigw_service.config import APP_CONFIG, Secrets
from aigw_service.exceptions import StopEventError
from aigw_service.logger import ContextVarsContainer, LoggerConfigurator

_STOP_EVENT_MARKER = b"temporarily unavailable due to technical reasons"


def _wrap_llm_with_stop_event(llm, logger) -> object:
    """Обернуть LLM для обнаружения StopEvent (ТН08).

    Перехватывает ``ForbiddenError`` от GigaChat SDK. Если тело ответа содержит
    маркер временной недоступности — поднимает ``StopEventError``.
    Реализовано в инфраструктурном слое (Rule 1 ТН08): бизнес-логика не должна
    самостоятельно детектировать стоп-сигнал.
    """
    original_invoke = llm.invoke
    original_ainvoke = getattr(llm, "ainvoke", None)

    def _check_stop(error: Exception) -> None:
        if isinstance(error, ForbiddenError):
            content = (error.content or b"").lower()
            if _STOP_EVENT_MARKER in content:
                logger.error(
                    "StopEvent detected: HTTP 403 from %s, body=%s, timestamp=%s",
                    error.url,
                    error.content,
                    datetime.now(UTC).isoformat(),
                )
                raise StopEventError(
                    user_message="Выбранная модель GigaChat временно недоступна. Попробуйте позже.",
                    url=str(error.url),
                    status_code=403,
                    reason=str(error.content),
                ) from error

    def wrapped_invoke(*args, **kwargs):
        try:
            return original_invoke(*args, **kwargs)
        except Exception as e:
            _check_stop(e)
            raise

    llm.invoke = wrapped_invoke

    if original_ainvoke is not None:

        async def wrapped_ainvoke(*args, **kwargs):
            try:
                return await original_ainvoke(*args, **kwargs)
            except Exception as e:
                _check_stop(e)
                raise

        llm.ainvoke = wrapped_ainvoke

    return llm


class AppContext(metaclass=Singleton):
    @property
    def logger(self):
        return self._logger_manager.async_logger

    def __init__(self, secrets: Secrets):
        # App
        self.timezone = pytz.timezone(secrets.app.timezone)
        self.debug_mode = secrets.app.debug
        self.openapi_version = secrets.app.openapi_version
        self.app_metadata = secrets.app.metadata

        # Logger
        self.context_vars_container = ContextVarsContainer()
        self._logger_manager = LoggerConfigurator(
            log_lvl=secrets.log.log_lvl,
            log_file_path=secrets.log.log_file_abs_path,
            metric_file_path=secrets.log.metric_file_abs_path,
            audit_file_path=secrets.log.audit_file_abs_path,
            audit_host_ip=secrets.log.audit_host_ip,
            audit_host_uid=secrets.log.audit_host_uid,
            context_vars_container=self.context_vars_container,
            timezone=self.timezone,
            rotation=secrets.log.log_rotation,
        )

        # ТН05 Rule 14: единый источник retry-параметров — config, а не хардкод
        logging.getLogger("gigachat.retry").setLevel(logging.WARNING)

        # Модель
        self._model_to_use = APP_CONFIG.app.model_to_use
        self._gigachat_base_params = None
        self._gigachat_credentials = None
        self._gigachat_max_retries = secrets.gigachat.max_retries
        self._gigachat_retry_backoff_factor = secrets.gigachat.retry_backoff_factor
        self.gigachat_embeddings = None
        self._ollama_kwargs = None

        if self._model_to_use == "GIGACHAT":
            self._gigachat_base_params = secrets.gigachat.base_params
            self.gigachat_embeddings = GigaChatEmbeddings(**self._gigachat_base_params)
        elif self._model_to_use == "GIGACHAT_TOKEN":
            self._gigachat_credentials = secrets.gigachat.credentials
        elif self._model_to_use == "OLLAMA":
            self._ollama_kwargs = {
                "base_url": secrets.ollama.base_url,
                "model": secrets.ollama.model_name,
                "temperature": secrets.ollama.temperature,
                "timeout": secrets.ollama.timeout,
            }

        # Хранилище для агента
        self.agent_store: InMemoryStore = InMemoryStore()

        # Pangolin (опционально)
        self.pangolin: AsyncPangolinClient | None = None
        self._client_registry: tuple[BaseAsyncInterface, ...] = ()

        # Если используется Pangolin, инициализируем подключение
        if APP_CONFIG.app.store_to_use == "PANGOLIN":
            self.pangolin = AsyncPangolinClient(
                logger=self.logger,
                conninfo=secrets.pangolin.db_uri,
                timeout=60,
                min_connections=5,
                max_connections=10,
            )
            self._client_registry = (self.pangolin,)

        # Agent memory
        self.agent_memory: AsyncAgentMemory = AsyncAgentMemory(logger=self.logger)
        # Устанавливаем хранилище (по умолчанию InMemoryStore)
        self.agent_memory.store = self.agent_store

        self.logger.info("App context initialized.")

    def get_logger(self):
        return self.logger

    def get_context_vars_container(self):
        return self.context_vars_container

    def get_pytz_timezone(self):
        return self.timezone

    def get_gigachat_base_params(self):
        return self._gigachat_base_params

    def get_gigachat_embeddings(self):
        return self.gigachat_embeddings

    def get_ollama_kwargs(self):
        return self._ollama_kwargs

    def create_llm(self, model_name: str = "GigaChat-2-Pro", **kwargs):
        """Создаёт LLM в зависимости от MODEL_TO_USE.

        Retry-параметры (max_retries, retry_backoff_factor) читаются из config
        для всех веток — ТН05 Rule 14 (без копипасты).
        Обёрнут StopEvent-wrapper'ом — ТН08.
        """
        from langchain_gigachat import GigaChat

        from aigw_service.api.v1.tools import TOOLS

        function_ranker = {"enabled": True, "top_n": len(TOOLS)}

        if self._model_to_use == "GIGACHAT":
            llm = GigaChat(
                **self._gigachat_base_params,
                model=model_name,
                timeout=kwargs.get("timeout", 60),
                function_ranker=function_ranker,
                top_p=1.0,
                repetition_penalty=1.0,
            )
        elif self._model_to_use == "GIGACHAT_TOKEN":
            llm = GigaChat(
                credentials=self._gigachat_credentials,
                verify_ssl_certs=False,
                model=model_name,
                timeout=kwargs.get("timeout", 60),
                temperature=0.000001,
                max_tokens=8192,
                max_retries=self._gigachat_max_retries,
                retry_backoff_factor=self._gigachat_retry_backoff_factor,
                function_ranker=function_ranker,
                top_p=1.0,
                repetition_penalty=1.0,
            )
        elif self._model_to_use == "OLLAMA":
            from langchain_ollama import ChatOllama

            llm = ChatOllama(
                base_url=self._ollama_kwargs["base_url"],
                model=self._ollama_kwargs.get("model", kwargs.get("model", "llama3")),
                temperature=self._ollama_kwargs.get("temperature", kwargs.get("temperature", 0.000001)),
                timeout=self._ollama_kwargs.get("timeout", kwargs.get("timeout", 60)),
            )
        else:
            raise ValueError(f"Unknown MODEL_TO_USE: {self._model_to_use}")

        return _wrap_llm_with_stop_event(llm, self.logger)

    async def _check_llm_connection(self):
        if self._model_to_use in ("GIGACHAT", "GIGACHAT_TOKEN"):
            await self._check_gigachat_connection()
        elif self._model_to_use == "OLLAMA":
            await self._check_ollama_connection()

    async def _check_gigachat_connection(self):
        if self._model_to_use == "GIGACHAT":
            gigachat = GigaChat(**self._gigachat_base_params)
        elif self._model_to_use == "GIGACHAT_TOKEN":
            gigachat = GigaChat(
                credentials=self._gigachat_credentials,
                verify_ssl_certs=False,
                max_retries=self._gigachat_max_retries,
                retry_backoff_factor=self._gigachat_retry_backoff_factor,
            )
        else:
            return
        try:
            self.logger.info(f"Attempt to connect to GigaChat at host {gigachat.base_url}.")
            models = await gigachat.aget_models()
            if self.debug_mode:
                print("=" * 80)
                self.logger.debug(f"Available models: {[model.id_ for model in models.data]}")
                print("=" * 80)
            self.logger.info(f"Connection to GigaChat at host {gigachat.base_url} successfully established.")
        except (RequestError, AuthenticationError) as e:
            self.logger.error(f"Error connecting to GigaChat at host {gigachat.base_url}: {e}")

    async def _check_ollama_connection(self):
        url = f"{self._ollama_kwargs['base_url'].rstrip('/')}/api/tags"
        model_name = self._ollama_kwargs["model"]
        try:
            self.logger.info(f"Attempt to connect to Ollama at {url}.")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                if self.debug_mode:
                    print("=" * 80)
                    self.logger.debug(f"Available Ollama models: {models}")
                    print("=" * 80)
                if model_name not in models:
                    self.logger.warning(f"Model '{model_name}' not found in Ollama. Available: {models}")
            self.logger.info(f"Connection to Ollama at {self._ollama_kwargs['base_url']} successfully established.")
        except httpx.HTTPError as e:
            self.logger.error(f"Error connecting to Ollama at {self._ollama_kwargs['base_url']}: {e}")

    async def on_startup(self):
        self.logger.info("Application is starting up.")

        # Проверяем соединение с моделью
        await self._check_llm_connection()

        # Запускаем клиентов (Pangolin если используется)
        for client in self._client_registry:
            await client.on_startup()

        # Инициализируем память в агенте после подключения к БД
        # Если используется Pangolin, подключаем его пул к agent_memory
        if self.pangolin and self.pangolin.pool:
            self.agent_memory.set_connection(self.pangolin.pool)
            self.logger.info("Pangolin connection established for agent memory.")
        else:
            self.logger.info("Using InMemoryStore for agent memory (STORE_TO_USE=MEMORY).")

        self.logger.info("All connections checked. Application is up and ready.")

    async def on_shutdown(self):
        self.logger.info("Application is shutting down.")

        # Останавливаем клиентов
        for client in self._client_registry:
            await client.on_shutdown()

        self._logger_manager.remove_logger_handlers()


APP_CTX = AppContext(APP_CONFIG)


__all__ = [
    "APP_CTX",
]
