"""ContextMeter (AR-Q6, spec §9 правило 6, S2.7 design D5): size of every context layer before each model call.

The innermost ``wrap_model_call`` of the stack: it sees the request exactly as the model gets it — after history
compression and with every prompt block the outer middleware appended. One ``CONTEXT LAYERS`` log line per call;
a warning when the total is over ``AGENT_MAX_PROMPT_TOKENS``. Tokens are approximate, no network (design D3).
"""

import json
import math
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from aigw_service.api.v2.middleware.summarization import CHARS_PER_TOKEN, SUMMARY_SOURCE
from aigw_service.api.v2.middleware.turn_summary import MEMORY_HEADER, PLAN_TOOL
from aigw_service.api.v2.prompts.middleware import SKILLS_SYSTEM_PROMPT, TODO_SYSTEM_PROMPT
from aigw_service.api.v2.turn.card import CARD_HEADER
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

LAYERS = (
    "instructions",
    "card",
    "skills_catalog",
    "skill_text",
    "plan",
    "calls_results",
    "memory",
    "history_summary",
    "dialog",
    "tools",
)
SYSTEM_HEADERS = (
    (CARD_HEADER, "card"),
    (SKILLS_SYSTEM_PROMPT.splitlines()[0], "skills_catalog"),
    (TODO_SYSTEM_PROMPT.splitlines()[0], "plan"),
    (MEMORY_HEADER, "memory"),
)
SKILLS_PREFIX = "/skills/"
READ_FILE = "read_file"


def text_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def _blocks(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        yield content
        return
    for block in content or []:
        if isinstance(block, str):
            yield block
        elif isinstance(block, dict) and block.get("type") == "text":
            yield str(block.get("text", ""))


def _system_layer(text: str) -> str:
    head = text.lstrip()
    return next((layer for header, layer in SYSTEM_HEADERS if head.startswith(header)), "instructions")


def _call_layer(name: str, args: dict[str, Any]) -> str:
    if name == PLAN_TOOL:
        return "plan"
    if name == READ_FILE and str(args.get("file_path", "")).startswith(SKILLS_PREFIX):
        return "skill_text"
    return "calls_results"


def _message_layers(messages: list[BaseMessage], sizes: dict[str, int]) -> None:
    calls: dict[str, str] = {}
    for message in messages:
        text = "".join(_blocks(message.content))
        if isinstance(message, AIMessage):
            sizes["dialog"] += text_tokens(text)
            for call in message.tool_calls:
                layer = _call_layer(call["name"], call.get("args") or {})
                calls[call.get("id") or ""] = layer
                sizes[layer] += text_tokens(call["name"] + json.dumps(call.get("args") or {}, ensure_ascii=False))
        elif isinstance(message, ToolMessage):
            layer = calls.get(message.tool_call_id) or _call_layer(message.name or "", {})
            sizes[layer] += text_tokens(text)
        elif isinstance(message, HumanMessage):
            source = message.additional_kwargs.get("lc_source")
            layer = {"pinned_skill": "skill_text", SUMMARY_SOURCE: "history_summary"}.get(source, "dialog")
            sizes[layer] += text_tokens(text)
        else:
            sizes["dialog"] += text_tokens(text)


def _tool_tokens(tool: Any) -> int:
    try:
        schema = tool if isinstance(tool, dict) else convert_to_openai_tool(tool)
    except Exception:  # pylint: disable=broad-exception-caught  # a size estimate never stops a model call
        schema = {"name": getattr(tool, "name", str(tool))}
    return text_tokens(json.dumps(schema, ensure_ascii=False))


def measure_layers(request: ModelRequest) -> dict[str, int]:
    """Tokens per layer of the request (design D5); ``total`` is their sum."""
    sizes = dict.fromkeys(LAYERS, 0)
    if request.system_message is not None:
        for text in _blocks(request.system_message.content):
            sizes[_system_layer(text)] += text_tokens(text)
    _message_layers(list(request.messages), sizes)
    sizes["tools"] = sum(_tool_tokens(t) for t in request.tools or [])
    sizes["total"] = sum(sizes[layer] for layer in LAYERS)
    return sizes


def call_number(state: dict[str, Any]) -> int:
    """1-based model call of the turn: the turn's model answers so far plus one."""
    messages = state.get("messages") or []
    start = state.get("turn_message_id")
    index = next((i for i, m in enumerate(messages) if start and m.id == start), -1)
    return 1 + sum(isinstance(m, AIMessage) for m in messages[index + 1 :])


class ContextMeter(AgentMiddleware):
    """Last in the stack: no ``after_*`` hooks, so its place does not reorder the guards."""

    def __init__(self, max_prompt_tokens: int) -> None:
        super().__init__()
        self.max_prompt_tokens = max_prompt_tokens

    def _log(self, request: ModelRequest) -> None:
        layers = measure_layers(request)
        context = getattr(request.runtime, "context", None)
        record = {
            "trace_id": getattr(context, "trace_id", ""),
            "turn_id": request.state.get("turn_id"),
            "call": call_number(request.state),
            "layers": layers,
        }
        logger.info("CONTEXT LAYERS {}", json.dumps(record, ensure_ascii=False))
        if layers["total"] > self.max_prompt_tokens:
            logger.warning(
                "Context over budget: {} tokens > AGENT_MAX_PROMPT_TOKENS {}", layers["total"], self.max_prompt_tokens
            )

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        self._log(request)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        self._log(request)
        return await handler(request)
