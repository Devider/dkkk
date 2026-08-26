import json
from functools import wraps

# Ленивый импорт aef_tracing — модуль может отсутствовать при локальной разработке
try:
    from aef_tracing import *  # noqa: F401,F403
    from aef_tracing.span_processors import session_id_cvar
    _TRACING_SPANS_AVAILABLE = True
except ImportError:
    session_id_cvar = None  # type: ignore[assignment]
    _TRACING_SPANS_AVAILABLE = False

from aigw_service.context import APP_CTX


def trace_agent_call(input_data=None):
    """
    Декоратор для трейсинга вызова агента с помощью aef_agent_start.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with aef_agent_start(input=input_data) as started_agent_controller:
                # Вызов основной логики (например, agent.invoke())
                result = func(*args, **kwargs)
                # Добавление результата в трейсинг
                started_agent_controller.add_output_result(output=result)
                return result

        return wrapper

    return decorator


def trace_output_request(
    path,
    method,
    span_name="output_request",
):
    """
    Декоратор для трейсинга внешнего запроса с помощью aef_output_request.

    Request объект извлекается из контекста (middleware).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Получаем Request из контекста
            request = APP_CTX.get_context_vars_container().get_request()
            if request is None:
                raise ValueError("Request object not found in context. Make sure log_requests middleware is applied.")

            # Получаем заголовки
            req_headers = dict(request.headers)
            if not req_headers:
                req_headers = {"Content-Type": "application/json"}

            # Получаем тело запроса
            try:
                raw_body = await request.body()
                req_body = json.loads(raw_body.decode()) if raw_body else {}
            except Exception:
                req_body = {}

            if not req_body:
                req_body = {"status": "success"}

            session_id = req_headers.get("x-session-id")
            session_id_cvar.set(session_id)

            with aef_output_request(
                span_name=span_name, headers=req_headers, body=req_body, path=path, method=method
            ) as output_request_controller:
                # Вызов основной логики функции
                response_body, response_headers = func(*args, **kwargs)
                # Добавление ответа в трейсинг
                output_request_controller.add_response(headers=response_headers, body=response_body)
                return response_body, response_headers

        return wrapper

    return decorator


def trace_input_request(
    path,
    method,
    span_name="input_request",
):
    """
    Декоратор для трейсинга входящего запроса.
    Автоматически устанавливает session_id из заголовков и добавляет ответ в трейсинг.

    Request объект извлекается из контекста (middleware).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Получаем Request из контекста (middleware его туда положил)
            request = APP_CTX.get_context_vars_container().get_request()
            if request is None:
                raise ValueError("Request object not found in context. Make sure log_requests middleware is applied.")

            # Получаем заголовки
            req_headers = dict(request.headers)
            if not req_headers:
                req_headers = {"Content-Type": "application/json"}

            # Получаем тело запроса
            try:
                raw_body = await request.body()
                req_body = json.loads(raw_body.decode()) if raw_body else {}
            except Exception:
                req_body = {}

            if not req_body:
                req_body = {"status": "success"}

            session_id = req_headers.get("x-session-id")
            if session_id:
                session_id_cvar.set(session_id)

            with aef_input_request(
                span_name=span_name, headers=req_headers, body=req_body, path=path, method=method
            ) as input_request_controller:
                result = await func(*args, **kwargs)
                input_request_controller.add_response(headers={}, body=result)
                return result

        return wrapper

    return decorator


def trace_kafka_produce(topic, kafka_cluster, bootstrap_servers, span_name="produce_message"):
    """
    Декоратор для трейсинга отправки сообщения в Kafka.

    Request объект извлекается из контекста (middleware).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Получаем Request из контекста
            request = APP_CTX.get_context_vars_container().get_request()

            # Значения по умолчанию
            bs_servers = bootstrap_servers or ["host1.delta.sbrf.ru:9093", "host2.delta.sbrf.ru:9093"]

            if request is not None:
                req_headers = dict(request.headers)
                session_id = req_headers.get("x-session-id")
                if session_id:
                    session_id_cvar.set(session_id)

                try:
                    raw_body = await request.body()
                    req_body = json.loads(raw_body.decode()) if raw_body else {}
                except Exception:
                    req_body = {}
            else:
                req_headers = {}
                req_body = {}

            with aef_kafka_produce(
                span_name=span_name,
                headers=req_headers,
                body=req_body,
                topic=topic,
                kafka_cluster=kafka_cluster,
                bootstrap_servers=bs_servers,
            ) as kafka_produce_controller:
                # Вызов основной логики отправки сообщения
                result = func(*args, **kwargs)
                return result

        return wrapper

    return decorator


def trace_custom_span(span_attributes=None):
    """
    Декоратор для создания кастомного спана с помощью aef_custom_span.
    Автоматически добавляет результат функции в атрибуты спана.

    Request объект извлекается из контекста (middleware).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Получаем Request из контекста
            request = APP_CTX.get_context_vars_container().get_request()

            attrs = span_attributes.copy() if span_attributes else {}

            if request is not None:
                session_id = request.headers.get("x-session-id")
                if session_id:
                    session_id_cvar.set(session_id)

            with aef_custom_span(span_attributes=attrs) as span_controller:
                # Выполняем основную логику функции
                result = func(*args, **kwargs)
                # Добавляем результат в атрибуты спана
                span_controller.add_span_attributes(**{"aef.output": result})
                return result

        return wrapper

    return decorator
