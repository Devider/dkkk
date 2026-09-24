import logging
import ssl
from typing import Any
from urllib.parse import urlparse

from httpx import Client, HTTPError
from langfuse import Langfuse  # Версия Langfuse: 3.11.2
from langfuse.api.core import ApiError
from langfuse.langchain import CallbackHandler

from aigw_modules.base import BaseSyncInterface
from aigw_modules.exceptions import HealthCheckError


class LangfuseClient(BaseSyncInterface):
    """
    Синхронный класс для работы с LangFuse.

    Args:
        logger (logging.Logger): Логгер
        public_key (str): Публичный ключ Langfuse
        secret_key (str): Секретный ключ Langfuse
        base_url (str): URL для Langfuse
        debug (bool): Режим отладки Langfuse. По умолчанию False
        tracing_enabled (bool): Включение трейсинга Langfuse. По умолчанию True
        ca_bundle (str | None): Путь к файлу сертификата CA. По умолчанию None
        certs (tuple[str, str] | None): Кортеж с TLS сертификатами.
            Включает минимум 2 сертификата в следующем порядке: tls_cert, tls_key. По умолчанию None
        **langfuse_kwargs (Any): Дополнительные параметры Langfuse

    Raises:
        RuntimeError: Если возникли проблемы инициализации Langfuse.
        ValueError: Если строка подключения к LangFuse некорректна.
        HealthCheckError: Если проверка подключения к LangFuse не пройдена.

    Examples:
        Инициализация и использование:
        .. code-block:: python

            import logging

            from langgraph.prebuilt import create_react_agent

            from aigw_modules.hub_services.langfuse import LangfuseClient


            langfuse = LangfuseClient(
                logger=logging.Logger(...),
                public_key="pk-lf-...",
                secret_key="sk-lf-...",
                base_url="https://langfuse.com/langfuse",
                ca_bundle="./ca_bundle.pem",
                certs=("./tls.pem", "./key.pem"),
            )

            # Инициализируем клиента
            langfuse.init_client()

            # Создаем агента
            my_agent = create_react_agent(...)

            # Вызываем агента с CallbackHandler
            result = my_agent.invoke(
                {"messages": [{"role": "user", "content": "Привет, Мир!"}]},
                config={"callbacks": [langfuse.callback_handler]},
            )

        Динамическая подстановка параметров в трейс:
        .. code-block:: python
            import logging
            import uuid

            from aigw_modules.hub_services.langfuse import LangfuseClient

            langfuse = LangfuseClient(
                logger=logging.Logger(...),
                public_key="pk-lf-...",
                secret_key="sk-lf-...",
                base_url="https://langfuse.com/langfuse",
                ca_bundle="./ca_bundle.pem",
                certs=("./tls.pem", "./key.pem"),
            )

            # Инициализируем клиента
            langfuse.init_client()

            # Создаем агента
            my_agent = create_react_agent(...)

            # Кастомизируем трейс вызова агента
            with langfuse.client.start_as_current_observation(
                as_type="span",
                name="my-agent",
            ) as span:
                input = {"messages": [{"role": "user", "content": "Привет!"}]}
                response = my_agent.invoke(
                    input=input,
                    config={"callbacks": [langfuse.callback_handler]},
                )
                span.update_trace(
                    input=input,
                    output=response,
                    user_id="user_1234",
                    session_id="session_123456789",
                    tags=["my-agent-tag"],
                    metadata={
                        "headers": {
                            "x-trace-id": str(uuid.uuid4()),
                            "x-agent-id": "CI0678456",
                        },
                    },
                )
    """

    ENDPOINT_HEALTH_CHECK = "/api/public/health"

    def __init__(
        self,
        logger: logging.Logger,
        public_key: str,
        secret_key: str,
        base_url: str,
        debug: bool = False,
        tracing_enabled: bool = True,
        ca_bundle: str = None,
        certs: tuple[str, str] | None = None,
        tls_verify: bool = True,
        **langfuse_kwargs: Any,
    ):
        """Инициализация класса LangfuseClient.

        Args:
            logger (logging.Logger): Логгер
            public_key (str): Публичный ключ Langfuse
            secret_key (str): Секретный ключ Langfuse
            base_url (str): URL для Langfuse
            debug (bool): Режим отладки Langfuse. По умолчанию False
            tracing_enabled (bool): Включение трейсинга Langfuse. По умолчанию True
            ca_bundle (str | None): Путь к файлу сертификата CA. По умолчанию None
            certs (tuple[str, str] | None): Кортеж с TLS сертификатами. По умолчанию None.
                Включает минимум 2 сертификата в следующем порядке: tls_cert, tls_key
            tls_verify (bool): Проверять TLS-сертификаты. По умолчанию True
            **langfuse_kwargs (Any): Дополнительные параметры Langfuse
        """
        self.logger = logger
        self._public_key = public_key

        self._base_url, self._api_path = self._parse_host_uri(base_url)
        self._langfuse_base_url = self._base_url + self._api_path

        # LangFuse клиент
        self._langfuse: Langfuse | None = None

        # CallbackHandler
        self._callback_handler: CallbackHandler | None = None

        # Конфиги для LangFuse
        langfuse_config = langfuse_kwargs.copy()
        langfuse_config["public_key"] = public_key
        langfuse_config["secret_key"] = secret_key
        langfuse_config["base_url"] = self._langfuse_base_url
        langfuse_config["debug"] = debug
        langfuse_config["tracing_enabled"] = tracing_enabled

        # SSL контекст
        if ca_bundle and tls_verify:
            self._ssl_ctx = self._create_ssl_context(ca_bundle, certs)
        else:
            self._ssl_ctx = False  # Отключаем проверку TLS

        # Httpx клиент для LangFuse
        self._httpx_kwargs = langfuse_config.pop("httpx_kwargs", {})
        self._httpx_client = Client(base_url=self._base_url, verify=self._ssl_ctx, **self._httpx_kwargs)
        langfuse_config["httpx_client"] = self._httpx_client

        # При отключённой проверке TLS отключаем OTel HTTP-экспортер
        self._tls_verify = tls_verify
        if not tls_verify:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
            langfuse_config["tracer_provider"] = TracerProvider(
                span_processor=SimpleSpanProcessor(ConsoleSpanExporter())
            )

        self._kwargs = langfuse_config

    @property
    def client(self) -> Langfuse:
        return self._langfuse

    @property
    def callback_handler(self) -> CallbackHandler:
        if self._callback_handler is None:
            self._callback_handler = self._get_callback_handler()
        return self._callback_handler

    @staticmethod
    def _parse_host_uri(host: str) -> tuple[str, str]:
        """Парсит URI хоста и возвращает базовый URL и путь API."""
        try:
            uri = urlparse(host)
            if not uri.hostname:
                raise ValueError("'host' parameter must contain valid 'hostname'")
            base_url = f"{uri.scheme}://{uri.hostname}" + (f":{uri.port}" if uri.port else "")
            api_path = uri.path
        except Exception as exp:
            raise ValueError(f"Got invalid host: {exp}") from exp
        return base_url, api_path

    @staticmethod
    def _create_ssl_context(
        ca_bundle: str | None = None,
        certs: tuple[str, str] | None = None,
    ) -> ssl.SSLContext | None:
        """Создает SSL контекст для безопасного соединения."""
        if ca_bundle:
            ssl_ctx = ssl.create_default_context(cafile=ca_bundle)
            ssl_ctx.verify_mode = ssl.CERT_OPTIONAL
            ssl_ctx.check_hostname = False
            if isinstance(certs, tuple) and all(certs[:2]):
                ssl_ctx.load_cert_chain(*certs)
            return ssl_ctx

    def _get_callback_handler(self) -> CallbackHandler:
        """Создает обработчик callback'ов Langfuse."""
        if not self._langfuse:
            raise RuntimeError("Langfuse client is not initialized. Call init_client() or on_startup() first.")

        return CallbackHandler(public_key=self._public_key)

    def init_client(self):
        """
        Инициализирует клиента LangFuse.
        """
        self.logger.info("Langfuse client initialization")
        self._langfuse = Langfuse(**self._kwargs)

    def on_startup(self) -> None:
        """
        Запускает клиента LangFuse с инициализацией и проверкой доступности сервера LangFuse.

        Raises:
            RuntimeError: Если произошла если Langfuse не доступен.
            HealthCheckError: Если база данных Pangolin недоступна.
        """
        self.logger.info(f"LangFuse client '{self._langfuse_base_url}' creating")
        try:
            self.init_client()
            self.health_check()
        except (RuntimeError, HealthCheckError) as exc:
            self.logger.error(f"LangFuse client '{self._langfuse_base_url}' failed to created: {exc}")
            self._safe_close()
            raise

    def on_shutdown(self) -> None:
        """
        Завершает работу клиента Langfuse.
        """
        self.logger.info(f"LangFuse client '{self._langfuse_base_url}' shutdown")
        self._safe_close()

    def health_check(self):
        """
        Проверяет соединение с сервером Langfuse.

        Raises:
            RuntimeError: Если клиент Langfuse не проинициализирован.
            HealthCheckError: Если Langfuse недоступен или некорректно настроен.
        """

        self.logger.info("LangFuse client healthcheck")

        if not self._langfuse:
            self.logger.error("Langfuse client is not initialized")
            raise RuntimeError("LangFuse client is not initialized")

        try:
            response = self._httpx_client.get(self._api_path + self.ENDPOINT_HEALTH_CHECK).raise_for_status()
            status = response.json().get("status")
            if status != "OK":
                raise HealthCheckError(
                    f"The health check request to the endpoint {self.ENDPOINT_HEALTH_CHECK} returned status '{status}'"
                )
            self._langfuse.auth_check()
            self.logger.info(f"LangFuse client '{self._langfuse_base_url}' was successfully created")

        except HTTPError as exc:
            self.logger.error(f"HTTP error during healthcheck to '{self._langfuse_base_url}': {exc}")
            raise HealthCheckError(http_url=self._langfuse_base_url, exc=exc) from exc

        except ApiError as exc:
            self.logger.error(f"API error during healthcheck to '{self._langfuse_base_url}': {exc}")
            raise HealthCheckError(http_url=self._langfuse_base_url, exc=exc) from exc

        except Exception as exc:
            self.logger.error(f"LangFuse client '{self._langfuse_base_url}' healthcheck was failed: {exc}")
            raise HealthCheckError(http_url=self._langfuse_base_url, exc=exc) from exc

    def _safe_close(self) -> None:
        """
        Безопасно закрывает клиента LangFuse и все CallbackHandlers.
        """
        self.logger.info(f"LangFuse client '{self._langfuse_base_url}' closing")
        if not self._langfuse:
            self.logger.warning(f"LangFuse client '{self._langfuse_base_url}' has not initialized")
            return
        try:
            if not self._httpx_client.is_closed:
                self._httpx_client.close()
        except Exception as exc:
            self.logger.error(f"LangFuse client '{self._langfuse_base_url}' error closing: {exc}")
        finally:
            self._langfuse = None
        self.logger.info(f"LangFuse client '{self._langfuse_base_url}' was successfully closed")
