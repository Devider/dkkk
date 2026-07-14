"""Unit-тесты для ТН05 (Retry) и ТН08 (StopEvent).

Тестируют инфраструктурный слой: context.py (_wrap_llm_with_stop_event),
exceptions.py (StopEventError), config (retry params from config not hardcoded).
"""

import logging
from unittest.mock import MagicMock

import pytest
from gigachat.exceptions import BadRequestError, ForbiddenError, ServerError

from aigw_service.context import _STOP_EVENT_MARKER, _wrap_llm_with_stop_event
from aigw_service.exceptions import StopEventError

# ============================================================================
# ТН08: StopEvent tests
# ============================================================================


def _make_forbidden(content: bytes = b"") -> ForbiddenError:
    """Create a ForbiddenError with given response body content."""
    return ForbiddenError(
        url="https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        status_code=403,
        content=content,
        headers=None,
    )


class _FakeLLM:
    """Minimal LLM mock with invoke/ainvoke methods."""

    def __init__(self, side_effect=None, return_value=None):
        self._side_effect = side_effect
        self._return_value = return_value

    def invoke(self, *args, **kwargs):
        if self._side_effect:
            raise self._side_effect
        return self._return_value

    async def ainvoke(self, *args, **kwargs):
        if self._side_effect:
            raise self._side_effect
        return self._return_value


@pytest.fixture
def mock_logger():
    return MagicMock()


class TestStopEventDetection:
    """ТН08 Rule 4: обработка 403 + 'temporarily unavailable'."""

    def test_stop_event_raised_on_403_with_marker(self, mock_logger):
        """403 + 'temporarily unavailable' → StopEventError."""
        llm = _FakeLLM(side_effect=_make_forbidden(_STOP_EVENT_MARKER))
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(StopEventError) as exc_info:
            wrapped.invoke("test")

        assert exc_info.value.status_code == 403
        assert "GigaChat" in exc_info.value.user_message
        assert exc_info.value.url is not None
        assert exc_info.value.timestamp is not None

    def test_stop_event_logged_at_error(self, mock_logger):
        """ТН08 Rule 7: лог на ERROR с datetime, reason, url."""
        llm = _FakeLLM(side_effect=_make_forbidden(_STOP_EVENT_MARKER))
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(StopEventError):
            wrapped.invoke("test")

        mock_logger.error.assert_called_once()
        log_call_args = mock_logger.error.call_args
        log_msg = str(log_call_args)
        assert "403" in log_msg
        assert "StopEvent" in log_msg

    def test_plain_403_without_marker_not_stop_event(self, mock_logger):
        """403 без маркера 'temporarily unavailable' → обычный ForbiddenError."""
        forbidden = _make_forbidden(b"Access denied")
        llm = _FakeLLM(side_effect=forbidden)
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(ForbiddenError):
            wrapped.invoke("test")

    def test_non_forbidden_error_passes_through(self, mock_logger):
        """Другие ошибки (400, 500) проходят без трансформации."""
        server_error = ServerError(
            url="https://example.com",
            status_code=503,
            content=b"Server error",
            headers=None,
        )
        llm = _FakeLLM(side_effect=server_error)
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(ServerError):
            wrapped.invoke("test")

    def test_bad_request_passes_through(self, mock_logger):
        """400 BadRequest не должен стать StopEvent."""
        bad_request = BadRequestError(
            url="https://example.com",
            status_code=400,
            content=b"Bad request",
            headers=None,
        )
        llm = _FakeLLM(side_effect=bad_request)
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(BadRequestError):
            wrapped.invoke("test")

    def test_stop_event_preserves_original_exception(self, mock_logger):
        """ТН08 Rule 8: оригинальная ошибка сохраняется в __cause__."""
        original = _make_forbidden(_STOP_EVENT_MARKER)
        llm = _FakeLLM(side_effect=original)
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(StopEventError) as exc_info:
            wrapped.invoke("test")

        assert exc_info.value.__cause__ is original

    def test_successful_invoke_not_intercepted(self, mock_logger):
        """Успешный вызов проходит без вмешательства wrapper'а."""
        llm = _FakeLLM(return_value="success")
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        result = wrapped.invoke("test")
        assert result == "success"
        mock_logger.error.assert_not_called()


class TestStopEventAsync:
    """ТН08: async path."""

    async def test_stop_event_raised_async(self, mock_logger):
        """ainvoke с 403 + marker → StopEventError."""
        llm = _FakeLLM(side_effect=_make_forbidden(_STOP_EVENT_MARKER))
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(StopEventError):
            await wrapped.ainvoke("test")

    async def test_plain_403_not_stop_event_async(self, mock_logger):
        """ainvoke с 403 без маркера → обычный ForbiddenError."""
        forbidden = _make_forbidden(b"Forbidden")
        llm = _FakeLLM(side_effect=forbidden)
        wrapped = _wrap_llm_with_stop_event(llm, mock_logger)

        with pytest.raises(ForbiddenError):
            await wrapped.ainvoke("test")


class TestStopEventErrorFields:
    """ТН08: поля StopEventError."""

    def test_stop_event_error_carries_all_fields(self):
        error = StopEventError(
            user_message="Model unavailable",
            url="https://gigachat.example.com/api",
            status_code=403,
            reason="Service down",
        )
        assert error.user_message == "Model unavailable"
        assert error.url == "https://gigachat.example.com/api"
        assert error.status_code == 403
        assert error.reason == "Service down"
        assert error.timestamp is not None

    def test_stop_event_error_is_exception(self):
        error = StopEventError("msg", "url")
        assert isinstance(error, Exception)


# ============================================================================
# ТН05: Retry config tests
# ============================================================================


class TestRetryConfig:
    """ТН05 Rule 14: retry-параметры из config, не хардкод."""

    def test_gigachat_settings_has_retry_fields(self):
        from aigw_service.config import APP_CONFIG

        assert hasattr(APP_CONFIG.gigachat, "max_retries")
        assert hasattr(APP_CONFIG.gigachat, "retry_backoff_factor")
        assert isinstance(APP_CONFIG.gigachat.max_retries, int)
        assert isinstance(APP_CONFIG.gigachat.retry_backoff_factor, float)

    def test_retry_params_not_hardcoded_defaults(self):
        """config/gigachat должен иметь meaningful defaults."""
        from aigw_service.config.gigachat.config import GigaChatSettings

        # Verify field defaults exist in the model fields
        fields = GigaChatSettings.model_fields
        assert "max_retries" in fields
        assert "retry_backoff_factor" in fields

    def test_base_params_includes_retry_config(self):
        """base_params property должен включать max_retries и retry_backoff_factor."""
        from aigw_service.config import APP_CONFIG

        params = APP_CONFIG.gigachat.base_params
        assert "max_retries" in params
        assert "retry_backoff_factor" in params

    def test_gigachat_retry_logger_at_warning(self):
        """ТН05 Rule 10: логгер gigachat.retry установлен на WARNING."""
        logger = logging.getLogger("gigachat.retry")
        assert logger.level <= logging.WARNING


class TestNoHardcodedRetry:
    """ТН05 Rule 9в: нет дублирования retry-логики."""

    def test_context_uses_config_for_retry(self):
        """context.py хранит retry-параметры из config, не хардкод."""
        from aigw_service.context import APP_CTX

        assert APP_CTX._gigachat_max_retries is not None
        assert APP_CTX._gigachat_retry_backoff_factor is not None
        # Values should come from config, not be hardcoded literals
        from aigw_service.config import APP_CONFIG

        assert APP_CTX._gigachat_max_retries == APP_CONFIG.gigachat.max_retries
        assert APP_CTX._gigachat_retry_backoff_factor == APP_CONFIG.gigachat.retry_backoff_factor

    def test_no_hardcoded_5_in_gigachat_token_branch(self):
        """GIGACHAT_TOKEN branch не должен использовать хардкод max_retries=5."""
        import inspect

        from aigw_service.context import AppContext

        source = inspect.getsource(AppContext.create_llm)
        # The literal "max_retries=5" should NOT appear in source
        assert "max_retries=5" not in source, (
            "GIGACHAT_TOKEN branch still hardcodes max_retries=5 — use config instead"
        )
        assert "retry_backoff_factor=0.5" not in source, (
            "GIGACHAT_TOKEN branch still hardcodes retry_backoff_factor=0.5 — use config instead"
        )

    def test_no_hardcoded_in_check_connection(self):
        """_check_gigachat_connection не должен хардкодить retry params."""
        import inspect

        from aigw_service.context import AppContext

        source = inspect.getsource(AppContext._check_gigachat_connection)
        assert "max_retries=5" not in source
        assert "retry_backoff_factor=0.5" not in source


class TestSDKRetryBehavior:
    """ТН05 Rule 3: SDK обрабатывает retry для правильных status codes."""

    def test_sdk_default_retry_codes_exclude_403(self):
        """403 НЕ должен быть в retry_on_status_codes (ТН08 Rule 5)."""
        from gigachat.settings import Settings

        default_codes = Settings.model_fields["retry_on_status_codes"].default
        assert 403 not in default_codes
        assert 429 in default_codes
        assert 500 in default_codes
        assert 503 in default_codes

    def test_sdk_default_max_retries_is_zero(self):
        """SDK default max_retries=0 (disabled) — мы включаем через config."""
        from gigachat.settings import Settings

        assert Settings.model_fields["max_retries"].default == 0
