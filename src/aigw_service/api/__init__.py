import contextlib
import typing as tp

from fastapi import FastAPI

from aigw_service.api.metric_router import router as metric_router
from aigw_service.api.middleware import log_requests
from aigw_service.api.os_router import router as service_router
from aigw_service.api.v1.router import router as main_router
from aigw_service.context import APP_CTX


@contextlib.asynccontextmanager
async def lifespan(_) -> tp.AsyncContextManager:  # type: ignore
    await APP_CTX.on_startup()
    yield
    await APP_CTX.on_shutdown()


app_main = FastAPI(
    title="AIGateWay GenAI Ready REST API template service",
    description=APP_CTX.app_metadata["description"],
    version=APP_CTX.app_metadata["version"],
    lifespan=lifespan,
)

# Версия OpenAPI
app_main.openapi_version = APP_CTX.openapi_version

# Middleware
app_main.middleware("http")(log_requests)

# Роутеры
app_main.include_router(service_router, tags=["Openshift aigw_service routes"])
app_main.include_router(metric_router, tags=["Like/dislike metric aigw_service routes"])
# Разработанные вами роутеры подключаются здесь
# Согласно GenAI Ready версия в пути должна соответствовать major версии всего сервиса
# Версия 1.x.y -> /v1
# Версия 2.x.y -> /v2
app_main.include_router(main_router, prefix="/api/v1", tags=["Эндпоинты вашего приложения"])

__all__ = [
    "app_main",
]
