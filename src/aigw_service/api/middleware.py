import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import gigachat.context as gc_ctx
from fastapi import Request, status
from starlette.concurrency import iterate_in_threadpool

from aigw_service.context import APP_CTX

NON_LOGGED_ENDPOINTS = (
    "/like",
    "/dislike",
    "/health",
    "/info",
    "/openapi.json",
    "/docs",
)

HEADERS_WHITE_LIST_TO_LOG = (
    "x-trace-id",
    "x-request-time",
    "x-client-id",
    "x-session-id",
    "x-user-id",
)


def _get_decoded_body(raw_body: bytes, message_type: str, logger):
    decoded_body = {}
    try:
        decoded_body = json.loads(raw_body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(f"{message_type} body is not json")
    return decoded_body


EVENT_STREAM = "text/event-stream"
_TERMINAL_EVENTS = ("answer", "error")


def _log_response(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    logger, request_path: str, headers_to_log: dict, decoded_response_body, status_code: int, start_time: float
) -> None:
    """Записи ответа: журнал, аудит, метрики ответов, длительности и статуса (порядок — как до S6.3)."""
    # Логируем ответ приложения и кидаем метрику количества ответов
    logger.info(
        "Outgoing response to client system",
        args={
            "headers": headers_to_log,
            "message": decoded_response_body,
        },
        message_type="response",
        path=request_path,
    )
    logger.metric(
        metric_name="aigw_service_responses_total",
        metric_value=1,
    )
    logger.audit(
        event_name="BusinessRequestFinished",
        event_params=json.dumps(
            decoded_response_body,
            ensure_ascii=False,
        ),
    )

    # Логируем время обработки запроса и кидаем метрику скорости ответа
    processing_time_ms = int(round(time.time() - start_time, 3) * 1000)
    logger.info(f"Request processing time for {request_path}: {processing_time_ms} ms")
    logger.metric(
        metric_name="aigw_service_process_duration_ms",
        metric_value=processing_time_ms,
    )

    # Кидаем метрику статуса обработки запроса
    if status_code < status.HTTP_400_BAD_REQUEST:
        logger.metric(
            metric_name="aigw_service_request_status_success_total",
            metric_value=1,
        )
    else:
        logger.metric(
            metric_name="aigw_service_request_status_failure_total",
            metric_value=1,
        )


def _is_event_stream(response) -> bool:
    return response.headers.get("content-type", "").startswith(EVENT_STREAM)


def _terminal_data(frame: str) -> dict | None:
    """``data`` терминального кадра SSE (``answer`` / ``error``), иначе ``None``."""
    event, data = "", None
    for line in frame.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = line[len("data:") :].strip()
    if event not in _TERMINAL_EVENTS or data is None:
        return None
    try:
        decoded = json.loads(data)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _logged_stream(body_iterator, on_end: Callable[[dict], None]) -> AsyncIterator[bytes]:
    """Отдаёт куски потока сразу; помнит только хвост текущего кадра и ``data`` терминального.

    ``on_end`` вызывается ровно один раз в ``finally``: по окончании потока, при ошибке и при разрыве.
    """
    tail = ""
    terminal: dict = {}
    try:
        async for chunk in body_iterator:
            yield chunk
            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            *frames, tail = (tail + text).split("\n\n")
            for frame in frames:
                data = _terminal_data(frame)
                if data is not None:
                    terminal = data
    finally:
        on_end(terminal)


async def log_requests(request: Request, call_next):
    start_time = time.time()
    logger = APP_CTX.get_logger()
    request_path = request.url.path

    # Достаем заголовки запроса
    allowed_headers_to_log = ((k, request.headers.get(k)) for k in HEADERS_WHITE_LIST_TO_LOG)
    headers_to_log = {
        header_name: header_value for header_name, header_value in allowed_headers_to_log if header_value
    }

    # Устанавливаем x-trace-id for GigaChat
    x_trace_id = request.get("x-trace-id")
    gc_ctx.trace_id_cvar.set(x_trace_id)

    APP_CTX.get_context_vars_container().set_context_vars(
        x_trace_id=headers_to_log.get("x-trace-id", ""),
        x_client_id=headers_to_log.get("x-client-id", ""),
        x_session_id=headers_to_log.get("x-session-id", ""),
        x_request_time=headers_to_log.get("x-request-time", ""),
        x_user_id=headers_to_log.get("x-user-id", ""),
    )

    # Сохраняем Request в контекст для использования декораторами
    APP_CTX.get_context_vars_container().set_request(request)

    try:
        # Логируем получение запроса
        if request_path in NON_LOGGED_ENDPOINTS:  # Не следует логировать эти эндпоинты
            response = await call_next(request)
            logger.debug(f"Processed request for {request_path} with code {response.status_code}")
        # Проверка на наличие заголовка x-trace-id в запросе
        elif headers_to_log.get("x-trace-id", None):
            # Получаем тело запроса
            raw_request_body = await request.body()
            request_body_decoded = _get_decoded_body(raw_request_body, "request", logger)
            # Логируем, кидаем событие в аудит, кидаем метрику о количестве запросов
            logger.info(
                f"Incoming {request.method}-request for {request_path}",
                args={
                    "headers": headers_to_log,
                    "message": request_body_decoded,
                },
                message_type="request",
                path=request_path,
            )
            client_id = headers_to_log.get("x-user-id", None)
            if client_id:
                logger.metric(
                    metric_name=f"aigw_service_user_{client_id}",
                    metric_value=1,
                )
            logger.metric(
                metric_name="aigw_service_requests_total",
                metric_value=1,
            )
            logger.audit(
                event_name="BusinessRequestReceived",
                event_params=json.dumps(
                    request_body_decoded,
                    ensure_ascii=False,
                ),
            )

            response = await call_next(request)

            # Поток (S6.3, §13 «Журнал запросов и поток»): тело не копится, записи ответа — после конца потока
            if _is_event_stream(response):
                headers_to_log["x-response-time"] = datetime.now(APP_CTX.get_pytz_timezone()).isoformat()
                for header, value in headers_to_log.items():
                    response.headers[header] = value
                response.body_iterator = _logged_stream(
                    response.body_iterator,
                    lambda body: _log_response(
                        logger, request_path, headers_to_log, body, response.status_code, start_time
                    ),
                )
                return response

            # Тело response - это итератор, по которому нельзя пройти повторно.
            # Один из вариантов решения - это собрать итератор повторно, что и представлено ниже
            response_body = [chunk async for chunk in response.body_iterator]
            response.body_iterator = iterate_in_threadpool(iter(response_body))

            # Добавляем заголовки ответа
            headers_to_log["x-response-time"] = datetime.now(APP_CTX.get_pytz_timezone()).isoformat()
            for header in headers_to_log:
                response.headers[header] = headers_to_log[header]

            response_body_extracted = response_body[0] if len(response_body) > 0 else b""
            decoded_response_body = _get_decoded_body(response_body_extracted, "response", logger)
            _log_response(
                logger, request_path, headers_to_log, decoded_response_body, response.status_code, start_time
            )
        else:
            # Логируем получение запроса без x-trace-id и обрабатываем его
            logger.info(f"Incoming {request.method}-request with no id for {request_path}")
            response = await call_next(request)
            logger.info(f"Request with no id for {request_path} processing time: {time.time() - start_time: .3f} s")
    finally:
        # Очищаем Request из контекста после обработки
        gc_ctx.trace_id_cvar.set(None)
        APP_CTX.get_context_vars_container().clear_request()

    return response


__all__ = [
    "log_requests",
]
