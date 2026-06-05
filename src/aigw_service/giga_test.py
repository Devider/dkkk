import asyncio
from datetime import timedelta

import pytz
from httpx import ConnectError, HTTPError  # Подключение необходимых типов исключений
from langchain_gigachat import GigaChat

from aigw_service.config import Secrets
from aigw_service.logger import ContextVarsContainer, LoggerConfigurator


class AppContext:
    def __init__(self, secrets: Secrets):
        # Настройки приложения
        self.debug_mode = secrets.app.debug
        self._gigachat_base_params = secrets.gigachat.base_params

        # Контейнер контекстных переменных и временная зона
        self.context_vars_container = ContextVarsContainer()
        self.timezone = pytz.timezone(secrets.app.timezone)

        self._logger_manager = LoggerConfigurator(
            log_lvl=secrets.log.log_lvl,
            log_file_path=secrets.log.log_file_abs_path,
            metric_file_path=secrets.log.metric_file_abs_path,
            audit_file_path=secrets.log.audit_file_abs_path,
            audit_host_ip=secrets.log.audit_host_ip,
            audit_host_uid=secrets.log.audit_host_uid,
            context_vars_container=self.context_vars_container,
            timezone=self.timezone,
            rotation=timedelta(hours=24),  # Интервал ротации лог-файлов
        )

        self.logger = self._logger_manager.async_logger
        self.logger.info("App context initialized.")

    async def check_gigachat_connection(self):
        gigachat = GigaChat(**self._gigachat_base_params)

        try:
            models = await gigachat.aget_models()
            self.logger.info(f"Models: {models}")

            text_query = "Привет, как дела?"
            response = await gigachat.ainvoke(text_query)
            self.logger.info(f"Result: {response}")

        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e, "response") else "Unknown"
            self.logger.error(f"HTTP Error: {str(e)}, Status Code: {status_code}")

        except ConnectError as ce:
            self.logger.error(f"Connection Error: {ce}")

        except Exception as ex:
            self.logger.critical(f"Unexpected error: {ex}", exc_info=True)

    async def startup(self):
        await self.check_gigachat_connection()

    async def shutdown(self):
        pass


if __name__ == "__main__":
    app_ctx = AppContext(Secrets())
    asyncio.run(app_ctx.startup())
