import typing as tp

import httpx
import pytest


@pytest.fixture(scope="session")
async def async_client() -> tp.AsyncGenerator[httpx.AsyncClient, None]:
    from aigw_service.api import app_main
    from aigw_service.config import APP_CONFIG
    from aigw_service.context import APP_CTX

    await APP_CTX.on_startup()
    async with httpx.AsyncClient(
        base_url=f"http://{APP_CONFIG.app.app_host}:{APP_CONFIG.app.app_port}",
        follow_redirects=True,
        app=app_main,
    ) as ac:
        yield ac
