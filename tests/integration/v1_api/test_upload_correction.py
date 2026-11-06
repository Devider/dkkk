"""Integration tests for the ``/upload`` correction flow (tasks 3.3/3.4).

End-to-end through the ASGI app: uploading a real model triggers the one-time
correction, keeps the original as ``_original``, and returns the correction
message in ``FileLoaderResponse.content``.

Requires LibreOffice + pyuno and the model fixture; skipped otherwise.
"""

import uuid
from pathlib import Path

import pytest

MODEL = Path(__file__).resolve().parents[3] / "models" / "06.10.2026.xlsx"


def _uno_available() -> bool:
    try:
        from aigw_service.api.v1.lo_backend import ensure_uno

        ensure_uno()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not MODEL.exists(), reason=f"model fixture missing: {MODEL}"),
    pytest.mark.skipif(not _uno_available(), reason="LibreOffice/pyuno not available"),
]

UPLOAD_HEADERS = {
    "x-trace-id": str(uuid.uuid4()),
    "x-client-id": "CI00163870",
    "x-request-time": "2026-10-15T12:00:00+03:00",
    "x-user-id": "12345678",
}


async def _upload_model(async_client, filename="06.10.2026.xlsx"):
    data = MODEL.read_bytes()
    files = {"file": (filename, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return await async_client.post("/api/v1/upload", files=files, headers=UPLOAD_HEADERS)


async def test_upload_returns_correction_result(async_client):
    response = await _upload_model(async_client)

    assert response.status_code == 200, response.text
    body = response.json()
    # Content is the correction result, not the fixed success string.
    assert "Файл был успешно сохранен." != body["content"]
    assert body["content"], body
    assert body["filename"].endswith(".xlsx")


async def test_upload_keeps_original_copy(async_client):
    response = await _upload_model(async_client)
    assert response.status_code == 200, response.text

    save_dir = Path(response.json()["save_dir"])
    stored = save_dir / response.json()["filename"]
    original = save_dir / f"user_file_{UPLOAD_HEADERS['x-user-id']}_original.xlsx"

    assert stored.exists()
    assert original.exists()
    # The corrected file differs from the original client file.
    assert stored.read_bytes() != original.read_bytes()


async def test_uploaded_model_reopens_with_fixed_choose(async_client):
    """The stored (corrected) file opens in a fresh workbook without Err:504."""
    from aigw_service.api.v1.excel_handler import ExcelWorkbook

    response = await _upload_model(async_client)
    assert response.status_code == 200, response.text
    stored = str(Path(response.json()["save_dir"]) / response.json()["filename"])

    with ExcelWorkbook(stored) as xl:
        value = xl.get_cell("Dashboard", "D34")
    assert not (isinstance(value, str) and value.startswith(("Ошибка", "Err"))), value
