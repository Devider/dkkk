"""SkillToolGate (FR-18, spec §11 «Активация» п. 3; design S3.4 D1): a hidden skill tool is refused with a hint.

``SkillsMiddleware`` attaches a skill tool to the call only while its skill is read and in context; otherwise the call
reaches the tool node without a tool and fails as an unknown tool. This middleware sits right inside
``SkillsMiddleware``: a call of a catalogue skill tool that is still detached gets ``skill_required`` with the path of
the skill to read. Names outside the catalogue pass through and keep the unknown-tool error (AR-Q14).
"""

from collections.abc import Awaitable, Callable, Mapping

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.middleware.files import SKILLS_ROUTE
from aigw_service.api.v2.tools.envelope import ErrorCode, failure, to_json


def skill_path(skill: str) -> str:
    return f"{SKILLS_ROUTE}{skill}/SKILL.md"


def skill_required(name: str, call_id: str, skill: str) -> ToolMessage:
    hint = f"Прочитай {skill_path(skill)} через read_file и повтори вызов."
    envelope = failure(
        ErrorCode.SKILL_REQUIRED, f"Инструмент {name} открывает skill {skill}: он ещё не прочитан", hint
    )
    return ToolMessage(to_json(envelope), tool_call_id=call_id, name=name, status="error")


class SkillToolGate(AgentMiddleware):
    """Refuses a call of a skill tool the model was not shown; ``tool_skills`` maps a tool name to its skill."""

    def __init__(self, tool_skills: Mapping[str, str]) -> None:
        super().__init__()
        self.tool_skills = dict(tool_skills)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call["name"]
        skill = self.tool_skills.get(name)
        if request.tool is not None or skill is None:
            return None
        return skill_required(name, request.tool_call["id"], skill)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else await handler(request)
