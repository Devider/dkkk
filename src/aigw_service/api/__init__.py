import contextlib
import typing as tp

from fastapi import FastAPI

from aigw_service.api.metric_router import router as metric_router
from aigw_service.api.middleware import log_requests
from aigw_service.api.os_router import router as service_router
from aigw_service.api.v1.router import router as main_router
from aigw_service.api.v2.factory import get_agent_v2
from aigw_service.api.v2.tools.engine_gate import default_gate
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX
from aigw_service.core.tracing.langsmith_guard import forbid_langsmith


@contextlib.asynccontextmanager
async def lifespan(_) -> tp.AsyncContextManager:  # type: ignore
    forbid_langsmith()  # AR-Q14: tracing only inside the contour
    await APP_CTX.on_startup()
    if APP_CONFIG.agent.engine == "v2":
        get_agent_v2()  # AR-Q15: a broken skill or agent wiring stops the start, not the first request
    yield
    await APP_CTX.on_shutdown()
    await default_gate().shutdown()  # AR-Q12: no pool worker or soffice outlives the service; lazy, no-op on v1


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
