"""Unit tests for the LangChain-callback-manager-driven tool tracing around LO calc dispatch.

``_dispatch_lo_calc`` drives an explicit ``on_tool_start``/``on_tool_end``/``on_tool_error`` run via
the callback manager inherited from the graph node's own ``RunnableConfig`` - not Langfuse's raw
``start_as_current_observation`` - because LangGraph runs each node's body in a freshly created
``asyncio.Task`` whose copied context does not carry the OTEL span Langfuse's ``CallbackHandler``
attaches in ``on_chain_start`` (verified empirically: that span showed up as a disconnected root
trace). The ``parent_run_id`` chain carried through ``config["callbacks"]`` nests correctly instead.

Covers:
- ``_dispatch_lo_calc``: drives on_tool_start/on_tool_end with a callback handler present, is a
  no-op when there are no callbacks, and drives on_tool_error (still propagating the exception)
  when the pooled call raises.
- ``OrchestratorTools._run_ift`` / ``_run_ema``: ``config`` is threaded through to
  ``_dispatch_lo_calc`` and its result flows into the returned ``ToolCallResult``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.callbacks.base import AsyncCallbackHandler

from aigw_service.api.v1.subagents import orchestrator_tools as ot


def _make_tools() -> tuple[ot.OrchestratorTools, MagicMock, MagicMock]:
    ift_agent = MagicMock()
    ema_agent = MagicMock()
    tools = ot.OrchestratorTools(ift_agent=ift_agent, ema_agent=ema_agent)
    return tools, ift_agent, ema_agent


class _FakeFuture:
    """Stands in for the ``concurrent.futures.Future`` returned by ``ProcessPoolExecutor.submit``."""

    def __init__(self, value: dict | None = None, exc: Exception | None = None):
        self._value = value
        self._exc = exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return self._value


class _RecordingHandler(AsyncCallbackHandler):
    """Records on_tool_start/on_tool_end/on_tool_error calls for assertions."""

    def __init__(self):
        self.events: list[tuple] = []

    async def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append(("start", kwargs.get("name"), kwargs.get("inputs")))

    async def on_tool_end(self, output, **kwargs):
        self.events.append(("end", output))

    async def on_tool_error(self, error, **kwargs):
        self.events.append(("error", error))


# ===================================================================
# _dispatch_lo_calc
# ===================================================================


class TestDispatchLoCalc:
    async def test_drives_tool_start_and_end_with_callback_handler(self):
        tools, _, _ = _make_tools()
        handler = _RecordingHandler()
        config = {"callbacks": [handler]}

        calc_dict = {"status": "SUCCESS", "result": "ok", "content": {}}
        tools._lo_pool = MagicMock()
        tools._lo_pool.submit.return_value = _FakeFuture(value=calc_dict)

        kwargs = {"file_name": "f.xlsx"}
        result = await tools._dispatch_lo_calc("analyze_excel_model", kwargs, config)

        assert result == calc_dict
        kinds = [e[0] for e in handler.events]
        assert kinds == ["start", "end"]
        _, name, inputs = handler.events[0]
        assert name == "analyze_excel_model"
        assert inputs == kwargs
        assert handler.events[1][1] == calc_dict

    async def test_noop_without_callbacks(self):
        tools, _, _ = _make_tools()
        calc_dict = {"status": "SUCCESS", "result": "ok", "content": {}}
        tools._lo_pool = MagicMock()
        tools._lo_pool.submit.return_value = _FakeFuture(value=calc_dict)

        result = await tools._dispatch_lo_calc("analyze_excel_model", {"a": 1}, None)

        assert result == calc_dict

    async def test_drives_tool_error_and_still_propagates(self):
        tools, _, _ = _make_tools()
        handler = _RecordingHandler()
        config = {"callbacks": [handler]}

        boom = RuntimeError("Unreachable output-targets")
        tools._lo_pool = MagicMock()
        tools._lo_pool.submit.return_value = _FakeFuture(exc=boom)

        with pytest.raises(RuntimeError, match="Unreachable output-targets"):
            await tools._dispatch_lo_calc("analyze_excel_model", {"a": 1}, config)

        kinds = [e[0] for e in handler.events]
        assert kinds == ["start", "error"]
        assert handler.events[1][1] is boom


# ===================================================================
# _run_ift / _run_ema: config threaded through to _dispatch_lo_calc
# ===================================================================


class TestRunIftConfigWiring:
    async def test_dispatch_lo_calc_called_with_config_and_result_flows_through(self):
        tools, ift_agent, _ = _make_tools()
        q = SimpleNamespace(target_value=1000.0, output_year=2025, output_name="EBITDA")
        resolved = [SimpleNamespace(equivalent_input_name="Цена меди")]
        ift_agent.ainvoke = AsyncMock(return_value={"q_analysis": q, "resolved_inputs": resolved})

        calc_dict = {"status": "SUCCESS", "result": "ok", "content": {}}
        tools._dispatch_lo_calc = AsyncMock(return_value=calc_dict)

        sentinel_config = {"callbacks": [], "marker": "sentinel"}
        result = await tools._run_ift("query", {}, {}, "file.xlsx", "u1", sentinel_config)

        tools._dispatch_lo_calc.assert_awaited_once()
        called_tool_name, _called_kwargs, called_config = tools._dispatch_lo_calc.call_args[0]
        assert called_tool_name == "analyze_model_inputs_for_target"
        assert called_config is sentinel_config
        assert result["status"] == "SUCCESS"


class TestRunEmaConfigWiring:
    async def test_dispatch_lo_calc_called_with_config_and_result_flows_through(self):
        tools, _, ema_agent = _make_tools()
        q = SimpleNamespace(
            year=2025,
            mentioned_outputs=[SimpleNamespace(equivalent_output_name="FCFF")],
        )
        resolved = [
            SimpleNamespace(
                equivalent_input_name="Цена метанола",
                range_config=SimpleNamespace(start_value=450.0, end_value=500.0, step=5.0),
            )
        ]
        ema_agent.ainvoke = AsyncMock(return_value={"q_analysis": q, "resolved_inputs": resolved})

        calc_dict = {"status": "SUCCESS", "result": "ok", "content": {}}
        tools._dispatch_lo_calc = AsyncMock(return_value=calc_dict)

        sentinel_config = {"callbacks": [], "marker": "sentinel"}
        result = await tools._run_ema("query", {}, {}, "file.xlsx", "u1", sentinel_config)

        tools._dispatch_lo_calc.assert_awaited_once()
        called_tool_name, _called_kwargs, called_config = tools._dispatch_lo_calc.call_args[0]
        assert called_tool_name == "analyze_excel_model"
        assert called_config is sentinel_config
        assert result["status"] == "SUCCESS"


# ===================================================================
# Exception from the pooled call still propagates unchanged (real _dispatch_lo_calc, no mock)
# ===================================================================


class TestDispatchErrorPropagation:
    async def test_run_ift_propagates_pool_exception(self):
        tools, ift_agent, _ = _make_tools()
        q = SimpleNamespace(target_value=1000.0, output_year=2025, output_name="EBITDA")
        resolved = [SimpleNamespace(equivalent_input_name="Цена меди")]
        ift_agent.ainvoke = AsyncMock(return_value={"q_analysis": q, "resolved_inputs": resolved})

        boom = RuntimeError("Unreachable output-targets")
        tools._lo_pool = MagicMock()
        tools._lo_pool.submit.return_value = _FakeFuture(exc=boom)

        with pytest.raises(RuntimeError, match="Unreachable output-targets"):
            await tools._run_ift("query", {}, {}, "file.xlsx", "u1", None)

    async def test_run_ema_propagates_pool_exception(self):
        tools, _, ema_agent = _make_tools()
        q = SimpleNamespace(
            year=2025,
            mentioned_outputs=[SimpleNamespace(equivalent_output_name="FCFF")],
        )
        resolved = [
            SimpleNamespace(
                equivalent_input_name="Цена метанола",
                range_config=SimpleNamespace(start_value=450.0, end_value=500.0, step=5.0),
            )
        ]
        ema_agent.ainvoke = AsyncMock(return_value={"q_analysis": q, "resolved_inputs": resolved})

        boom = RuntimeError("Unreachable output-targets")
        tools._lo_pool = MagicMock()
        tools._lo_pool.submit.return_value = _FakeFuture(exc=boom)

        with pytest.raises(RuntimeError, match="Unreachable output-targets"):
            await tools._run_ema("query", {}, {}, "file.xlsx", "u1", None)
