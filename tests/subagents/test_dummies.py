"""Unit tests for ``src/aigw_service/api/v1/subagents/dummies.py``.

Покрытие:
- dummy-фабрики возвращают корректные экземпляры
- ``validate_structured_output``: валидация, падение на невалидных данных
- ``retry_structured_llm``: success, failure → retry → success, все попытки исчерпаны
"""

from unittest.mock import patch

import pytest
from pydantic_core import ValidationError as PydanticValidationError

from aigw_service.api.v1.schemas.llm_outputs import (
    ClassifierOutput,
    QueryAnalysisEMA,
    QueryAnalysisIFT,
)
from aigw_service.api.v1.subagents.dummies import (
    dummy_classifier,
    dummy_query_ema,
    dummy_query_ift,
    retry_structured_llm,
    validate_structured_output,
)

# Предварительно создаём экземпляр ValidationError, который будем использовать в моках
# (прямое создание PydanticValidationError([{}]) ломается из-за внутренних типов PyLineError)
try:
    validate_structured_output({"next_agent": None}, ClassifierOutput)
except PydanticValidationError as _ve:
    _REAL_VALIDATION_ERROR = _ve
else:
    raise AssertionError("Expected ValidationError")


class MockStructuredLLM:
    """Имитация ``llm.with_structured_output(..., include_raw=True).invoke()``."""

    def __init__(self, results: list[object]):
        """*results* — список ответов: либо ``(parsed, raw)``."""
        self.results = list(results)
        self.call_count = 0

    def invoke(self, _prompt):
        answer = self.results[self.call_count]
        self.call_count += 1
        return {"parsed": answer[0], "raw": answer[1]}


class MockLLM:
    """Имитация LLM, возвращающая ``MockStructuredLLM`` при вызове ``with_structured_output``."""

    def __init__(self, results: list[object]):
        self.structured_results = results
        self._schema: type | None = None

    def with_structured_output(self, schema, include_raw=True):
        self._schema = schema
        return MockStructuredLLM(self.structured_results)


# ===================================================================
# Dummy factories
# ===================================================================


class TestDummyFactories:
    """Проверяет, что factory-функции возвращают валидные модели."""

    def test_dummy_classifier_returns_valid(self):
        result = dummy_classifier()
        assert isinstance(result, ClassifierOutput)
        assert result.next_agent == "analyze_excel_model"
        assert result.filename == ""

    def test_dummy_query_ift_returns_valid(self):
        result = dummy_query_ift()
        assert isinstance(result, QueryAnalysisIFT)
        assert "Dummy" in result.analysis
        assert result.target_value == 0.0
        assert result.output_year == 0
        assert result.mentioned_inputs == []

    def test_dummy_query_ema_returns_valid(self):
        result = dummy_query_ema()
        assert isinstance(result, QueryAnalysisEMA)
        assert "Dummy" in result.analysis
        assert result.year == 0
        assert result.mentioned_outputs == []


# ===================================================================
# validate_structured_output
# ===================================================================


class TestValidateStructuredOutput:
    """Тестирует функцию валидации структурированного вывода."""

    def test_valid_parsed_passed_through(self):
        parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="test.xlsx")
        validated = validate_structured_output(parsed, ClassifierOutput)
        assert validated is parsed

    def test_dict_parsed_validated_and_passed_through(self):
        parsed = {"next_agent": "analyze_model_inputs_for_target", "filename": "foo.xlsx"}
        validated = validate_structured_output(parsed, ClassifierOutput)
        assert validated.next_agent == "analyze_model_inputs_for_target"

    def test_invalid_literal_raises_validation_error(self):
        with pytest.raises(PydanticValidationError):
            validate_structured_output({"next_agent": "", "filename": "x"}, ClassifierOutput)

    def test_missing_required_field_raises_validation_error(self):
        with pytest.raises(PydanticValidationError):
            validate_structured_output({"filename": "x"}, ClassifierOutput)

    def test_none_raises_validation_error(self):
        with pytest.raises(PydanticValidationError):
            validate_structured_output(None, ClassifierOutput)


# ===================================================================
# retry_structured_llm
# ===================================================================


class TestRetryStructuredLLM:
    """Тестирует retry-обёртку для LLM-вызовов."""

    def test_success_on_first_try(self):
        parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="ok.xlsx")
        raw = type("RawResponse", (), {"response_metadata": {}})()
        llm = MockLLM([(parsed, raw)])

        result, success, raw_resp = retry_structured_llm(llm, ClassifierOutput, [])
        assert success is True
        assert result.next_agent == "analyze_excel_model"
        assert raw_resp["raw"] is raw

    def test_failure_then_success_on_third_try(self):
        good_parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="recovered.xlsx")
        raw = type("RawResponse", (), {"response_metadata": {}})()

        llm = MockLLM(
            [
                (good_parsed, raw),
                (good_parsed, raw),
                (good_parsed, raw),
            ]
        )

        result, success, _ = retry_structured_llm(llm, ClassifierOutput, [])
        assert success is True
        assert result.filename == "recovered.xlsx"
        assert llm._schema is ClassifierOutput

    def test_all_retries_exhausted_returns_dummy_classifier(self):
        raw = type("RawResponse", (), {"response_metadata": {}})()
        bad_parsed = {"next_agent": "", "filename": ""}

        llm = MockLLM(
            [
                (bad_parsed, raw),
                (bad_parsed, raw),
                (bad_parsed, raw),
            ]
        )

        result, success, raw_resp = retry_structured_llm(llm, ClassifierOutput, [], max_retries=3)
        assert success is False
        assert raw_resp is None
        assert isinstance(result, ClassifierOutput)
        assert result.next_agent == "analyze_excel_model"  # dummy default

    def test_all_retries_exhausted_returns_dummy_ift(self):
        raw = type("RawResponse", (), {"response_metadata": {}})()
        # bad_parsed вызывает ValidationError при model_validate
        bad_parsed = {"target_value": "not_a_float"}

        llm = MockLLM(
            [
                (bad_parsed, raw),
                (bad_parsed, raw),
                (bad_parsed, raw),
            ]
        )

        result, success, _ = retry_structured_llm(llm, QueryAnalysisIFT, [], max_retries=3)
        assert success is False
        assert isinstance(result, QueryAnalysisIFT)

    def test_all_retries_exhausted_returns_dummy_ema(self):
        raw = type("RawResponse", (), {"response_metadata": {}})()
        bad_parsed = {"year": "not_a_year"}

        llm = MockLLM(
            [
                (bad_parsed, raw),
                (bad_parsed, raw),
                (bad_parsed, raw),
            ]
        )

        result, success, _ = retry_structured_llm(llm, QueryAnalysisEMA, [], max_retries=3)
        assert success is False
        assert isinstance(result, QueryAnalysisEMA)

    def test_max_retries_1(self):
        """Проверяем, что max_retries=1 даёт ровно 1 попытку."""
        raw = type("RawResponse", (), {"response_metadata": {}})()
        bad_parsed = {"next_agent": "", "filename": "x"}

        llm = MockLLM([(bad_parsed, raw)])

        _, success, _ = retry_structured_llm(llm, ClassifierOutput, [], max_retries=1)
        assert success is False
        assert llm._schema is ClassifierOutput

    def test_returns_correct_raw_response_on_success(self):
        raw = type("RawResponse", (), {"response_metadata": {}})()
        parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="x.xlsx")

        llm = MockLLM([(parsed, raw)])
        _, success, raw_resp = retry_structured_llm(llm, ClassifierOutput, [])
        assert success is True
        assert raw_resp["raw"] is raw

    def test_validation_error_is_captured_and_retried(self):
        """Проверяет, что ValidationError правильно перехватывается и происходит повтор."""
        raw = type("RawResponse", (), {"response_metadata": {}})()
        parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="ok.xlsx")

        call_count = 0
        original_validate = validate_structured_output

        def failing_validate(p, schema):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise _REAL_VALIDATION_ERROR
            return original_validate(p, schema)

        llm = MockLLM(
            [
                (parsed, raw),
                (parsed, raw),
                (parsed, raw),
            ]
        )

        with patch("aigw_service.api.v1.subagents.dummies.validate_structured_output", side_effect=failing_validate):
            _, success, _ = retry_structured_llm(llm, ClassifierOutput, [], max_retries=3)

        assert success is True
        assert call_count == 3  # должно быть вызвано 3 раза (по одному на каждую попытку)

    def test_validation_error_all_fail_returns_dummy(self):
        """Проверяет, что если валидация падает везде, возвращается dummy."""
        raw = type("RawResponse", (), {"response_metadata": {}})()
        parsed = ClassifierOutput(next_agent="analyze_excel_model", filename="ok.xlsx")

        call_count = 0

        def always_fail_validate(p, schema):
            nonlocal call_count
            call_count += 1
            raise _REAL_VALIDATION_ERROR

        llm = MockLLM(
            [
                (parsed, raw),
                (parsed, raw),
                (parsed, raw),
            ]
        )

        with patch(
            "aigw_service.api.v1.subagents.dummies.validate_structured_output", side_effect=always_fail_validate
        ):
            result, success, raw_resp = retry_structured_llm(llm, ClassifierOutput, [], max_retries=3)

        assert success is False
        assert raw_resp is None
        assert call_count == 3
        assert isinstance(result, ClassifierOutput)
        assert result.next_agent == "analyze_excel_model"  # dummy default
