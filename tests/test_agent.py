#!/usr/bin/env python3
"""Тест загрузки Excel и вызова AI-агента через HTTP API."""

import http.client
import json
import os
import sys
import time
import tracemalloc

HOST = "localhost"
PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
LOG_FILE = os.path.join(BASE_DIR, "test_agent.log")
MODEL_PATH = os.path.join(PROJECT_DIR, "models", "model.xlsx")


def log(msg):
    """Печатает в stdout и пишет в лог-файл."""
    print(msg, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


# Обязательные заголовки
HEADERS = {
    "x-trace-id": "550e8400-e29b-41d4-a716-446655440000",
    "x-client-id": "CI12345678",
    "x-session-id": "550e8400-e29b-41d4-a716-446655440001",
    "x-user-id": "testuser",
    "x-request-time": "2026-06-08T12:00:00Z",
}


def request(method, path, body=None, headers=None):
    """HTTP-запрос через http.client."""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=180)
    req_headers = {**HEADERS, **(headers or {})}
    conn.request(method, path, body=body, headers=req_headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_upload():
    """Загрузка файла model.xlsx через /api/v1/upload."""
    log("\n" + "=" * 60)
    log("ТЕСТ 1: Загрузка model.xlsx")
    log("=" * 60)

    if not os.path.exists(MODEL_PATH):
        log(f"Файл не найден: {MODEL_PATH}")
        return None

    with open(MODEL_PATH, "rb") as f:
        file_data = f.read()

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body_parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="file"; filename="model.xlsx"',
        b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        b"",
        file_data,
        f"--{boundary}--".encode(),
    ]
    body = b"\r\n".join(body_parts)

    t0 = time.time()
    status, data = request(
        "POST",
        "/api/v1/upload",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    elapsed = time.time() - t0

    log(f"Статус: {status}")
    log(f"Ответ: {data.decode()}")
    log(f"Время: {elapsed:.2f} сек")
    return status


def test_invoke_agent():
    """Запуск AI-агента через /api/v1/invoke-agent."""
    log("\n" + "=" * 60)
    log("ТЕСТ 2: AI-агент — анализ debt/ebitda 2025")
    log("=" * 60)

    payload = json.dumps(
        {
            "message": (
                "Проанализируй model.xlsx со значением метанола 2025 (450,500) "
                "и шагом 5. Покажи мне значения debt/ebitda 2025 для этих значений."
            )
        }
    )

    t0 = time.time()
    status, data = request(
        "POST",
        "/api/v1/invoke-agent",
        body=payload,
        headers={"Content-Type": "application/json"},
    )
    elapsed = time.time() - t0

    log(f"Статус: {status}")
    log(f"Размер ответа: {len(data)} байт")
    log(f"Время: {elapsed:.2f} сек")

    if status == 200:
        # Сохраняем ZIP
        zip_path = os.path.join(PROJECT_DIR, "models", "response.zip")
        with open(zip_path, "wb") as f:
            f.write(data)
        log(f"ZIP сохранён: {zip_path}")
        log("Содержимое txt_response.txt:")
        try:
            import zipfile

            with zipfile.ZipFile(zip_path) as z:
                if "txt_response.txt" in z.namelist():
                    log(z.read("txt_response.txt").decode("utf-8"))
        except Exception as e:
            log(f"Не удалось прочитать ZIP: {e}")
    else:
        log(f"Ответ: {data.decode()[:500]}")


def measure_memory():
    """Замер памяти процесса."""
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = line.strip()
                if line.startswith("VmSize:"):
                    vms = line.strip()
            log("\n--- Память процесса ---")
            log(f"{rss}")
            log(f"{vms}")
    except Exception:
        pass


if __name__ == "__main__":
    # Очищаем лог-файл перед запуском
    with open(LOG_FILE, "w") as f:
        f.write(f"--- Тест запущен: {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    # Проверка, что сервер жив
    log("Проверка health...")
    status, data = request("GET", "/health")
    log(f"Health: {status} {data.decode()}")

    if status != 200:
        log("Сервер не отвечает. Запустите сначала.")
        sys.exit(1)

    tracemalloc.start()

    # Тест 1: Upload
    test_upload()
    mem_current, mem_peak = tracemalloc.get_traced_memory()
    log(f"Tracemalloc (после upload): {mem_peak / 1024:.1f} KB пик")
    measure_memory()

    # Тест 2: Invoke agent
    test_invoke_agent()
    mem_current, mem_peak = tracemalloc.get_traced_memory()
    log(f"Tracemalloc (после агента): {mem_current / 1024:.1f} KB текущая, {mem_peak / 1024:.1f} KB пик")
    measure_memory()

    log("\n✅ Тесты завершены.")
