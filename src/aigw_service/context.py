from datetime import UTC, datetime

import httpx
import pytz
from gigachat.exceptions import AuthenticationError, ForbiddenError
from httpx import RequestError
from langchain_gigachat import GigaChatEmbeddings

from aigw_modules.ai_agents.memory import AsyncAgentMemory
from aigw_modules.base import BaseAsyncInterface
from aigw_modules.hub_services.pangolin import AsyncPangolinClient
from aigw_service.base import Singleton
from aigw_service.config import APP_CONFIG, Secrets
from aigw_service.config import get_store as _get_store
from aigw_service.core.llm_executor import Agent
from aigw_service.core.tracing.tracing import AEFTracingHandler, TracingManager
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

    # Pydantic v2 BaseModel блокирует прямое присвоение не-полей, используем
    # object.__setattr__ для безопасного monkey-patching (см. README/dev-notes).
    object.__setattr__(llm, "invoke", wrapped_invoke)

    if original_ainvoke is not None:

        async def wrapped_ainvoke(*args, **kwargs):
            try:
                return await original_ainvoke(*args, **kwargs)
            except Exception as e:
                _check_stop(e)
                raise

        object.__setattr__(llm, "ainvoke", wrapped_ainvoke)

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

        # Реестр клиентов следующих базовому асинхронному интерфейсу BaseAsyncInterface из aigw_modules.base
        self._client_registry: list[BaseAsyncInterface] = []

        self._gigachat_base_params = secrets.gigachat.base_params
        self.gigachat_embeddings = GigaChatEmbeddings(**self._gigachat_base_params)
        self.llm = Agent(**self._gigachat_base_params, model=secrets.gigachat.model)

        # Platform V Search if enabled
        if secrets.platform_v_search.enabled:
            self.platform_v_search: PlatformVSearch = PlatformVSearch(
                logger=self.logger,
                hosts=secrets.platform_v_search.hosts,
                embedding=self.gigachat_embeddings,
                **secrets.platform_v_search.connection_params,
            )
            self.pvs_vectorstore = self.platform_v_search.get_vectorstore(secrets.platform_v_search.index_name)
            self._client_registry.append(self.platform_v_search)

        # Pangolin if enabled
        if secrets.pangolin.enabled:
            self.pangolin: AsyncPangolinClient = AsyncPangolinClient(
                logger=self.logger,
                conninfo=secrets.pangolin.db_uri,
            )
            self._client_registry.append(self.pangolin)

        # IDP GigaSearch if enabled
        if secrets.idp.enabled:
            self.idp: IDPService = IDPService(
                logger=self.logger,
                url=secrets.idp.base_url,
                source_uuid=secrets.idp.source_uuid,
                index_id=secrets.idp.index_id,
                retry_stop=secrets.idp.retry_stop,
                retry_wait=secrets.idp.retry_wait,
                request_timeout=secrets.idp.request_timeout,
                **secrets.idp.certs,
            )
            self._client_registry.append(self.idp)

        # LangFuse or AEF Tracing
        self.tracing: AEFTracingHandler | LangfuseClient = TracingManager(
            logger=self.logger,
            secrets=secrets,
        ).get_tracing()

        # Agent memory
        self.agent_memory: AsyncAgentMemory = AsyncAgentMemory(logger=self.logger)
        self.__secrets = secrets
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

    def get_tracing_cb_handler(self):
        return self.tracing

    async def _check_gigachat_connection(self):
        gigachat = self.llm
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
                resp.raise_if_error()
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

        # Запускаем фоновую задачу по проверке новой версии LLM
        if self.__secrets.app.preview_model_check:
            await self.llm.start_background_worker(self.logger)

        # Проверяем соединение с GigaChat
        await self._check_gigachat_connection()

        # Запускаем клиентов (Pangolin если используется)
        for client in self._client_registry:
            await client.on_startup()

        # Запускаем Langfuse
        if self.__secrets.langfuse.enabled:
            self.tracing.on_startup()

        # Инициализируем память в агенте после подключения к БД
        if self.__secrets.pangolin.enabled and self.pangolin.pool:
            self.agent_memory.set_connection(self.pangolin.pool)
            self.logger.info("Pangolin connection established for agent memory.")
        else:
            # Fallback to InMemoryStore when Pangolin is not available
            self.agent_memory.store = _get_store()
            self.logger.info("Using InMemoryStore for agent memory (STORE_TO_USE=MEMORY).")

        self.logger.info("All connections checked. Application is up and ready.")

    async def on_shutdown(self):
        self.logger.info("Application is shutting down.")

        # Останавливаем клиентов
        for client in self._client_registry:
            await client.on_shutdown()

        # Останавливаем фоновую задачу по проверке новой версии LLM
        if self.__secrets.app.preview_model_check:
            await self.llm.stop_background_worker()

        # Останавливаем Langfuse
        if self.__secrets.langfuse.enabled:
            self.tracing.on_shutdown()

        self._logger_manager.remove_logger_handlers()


APP_CTX = AppContext(APP_CONFIG)


__all__ = [
    "APP_CTX",
]
