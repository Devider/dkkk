import httpx
from constants import BASE_TEST_ENDPOINT, BASE_TEST_HEADER


async def test_health(async_client: httpx.AsyncClient):
    response: httpx.Response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "running"}


async def test_errors(async_client: httpx.AsyncClient):
    # GigaChat dependency failure
    test_req_body: dict = {
        "message": "Привет, как дела?",
    }

    response: httpx.Response = await async_client.post(
        BASE_TEST_ENDPOINT.format("predict"),
        json=test_req_body,
        headers=BASE_TEST_HEADER,
    )
    assert response.status_code == 404
