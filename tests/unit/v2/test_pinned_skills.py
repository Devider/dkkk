"""S3.5 (FR-19, AR-Q5): AGENT_PINNED_SKILLS — parsing, the factory's default, start-up checks, pinned skills in turns."""

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import Api, v2
from tests.unit.v2.test_covenant_sensitivity_skills import INDEX as TRACE_INDEX
from tests.unit.v2.test_covenant_sensitivity_skills import write_workbook
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_scenario_skills import tool_results
from tests.unit.v2.test_summarization import Session, SummarizingModel, compressed_calls, padding_reads

from aigw_service import api
from aigw_service.api.v2 import factory
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import ANSWER_SKILL, AgentFactory
from aigw_service.api.v2.middleware.summarization import ContextBudget
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.skills_check import SkillsError
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent import AgentSettings

TWO = (ANSWER_SKILL, "target-seeking")
ROOMY = ContextBudget(max_prompt_tokens=20000, trigger=0.7, keep=0.3)
"""A 6k-token verbatim tail holds both pinned skills of TWO (~2.6k); the test budget of S2.7 (3.6k) does not."""


def pinned_names(messages: list) -> list[str]:
    """Names of the skills pinned into these messages (deepagents ``lc_source="pinned_skill"``)."""
    return [
        m.additional_kwargs["skill"]["name"]
        for m in messages
        if m.additional_kwargs.get("lc_source") == "pinned_skill"
    ]


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []

    def warning(message: str, *args: Any) -> None:
        logged.append(message.format(*args))

    monkeypatch.setattr(factory.logger, "warning", warning)
    return logged


class TestSettings:
    """AR-Q5 «Список закреплённых skills»."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_PINNED_SKILLS", raising=False)
        assert AgentSettings().pinned_skills == (ANSWER_SKILL,)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (" economic-answer , target-seeking,economic-answer", TWO),
            ("target-seeking", ("target-seeking",)),
            ("", ()),
            (",", ()),
        ],
    )
    def test_parsed(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: tuple[str, ...]) -> None:
        monkeypatch.setenv("AGENT_PINNED_SKILLS", value)
        assert AgentSettings().pinned_skills == expected

    def test_list_is_normalized(self) -> None:
        assert AgentSettings(AGENT_PINNED_SKILLS=[" a", "b", "a", ""]).pinned_skills == ("a", "b")


class TestFactory:
    """Design D2–D3: the list comes from settings; an unknown name stops the build; no economic-answer — a warning."""

    @staticmethod
    def make(pinned: tuple[str, ...] | None = None, budget: ContextBudget | None = None) -> AgentFactory:
        return AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), pinned_skills=pinned, prompt_budget=budget)

    def test_default_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "pinned_skills", TWO)
        assert self.make().pinned_skills == TWO
        assert self.make(("target-seeking",)).pinned_skills == ("target-seeking",)  # an explicit list wins

    def test_unknown_name_stops_build(self) -> None:
        with pytest.raises(SkillsError, match="no-such-skill: закреплённый skill не найден"):
            self.make((ANSWER_SKILL, "no-such-skill")).build()

    @pytest.mark.parametrize("pinned", [("target-seeking",), ()])
    def test_without_answer_skill_warns(self, warnings: list[str], pinned: tuple[str, ...]) -> None:
        assert self.make(pinned).build() is not None
        assert len(warnings) == 1
        assert "без economic-answer" in warnings[0]
        assert "FR-12" in warnings[0]

    def test_pins_larger_than_tail_stop_build(self) -> None:
        """Design D5: pinned texts that do not fit the verbatim tail would make every call compress."""
        small = ContextBudget(max_prompt_tokens=20000, trigger=0.7, keep=0.1)  # 2k tail, TWO is ~2.6k
        with pytest.raises(SkillsError, match="не помещаются в дословный хвост истории ≈ 2000 токенов") as error:
            self.make(TWO, small).build()
        assert "economic-answer ≈" in str(error.value)
        assert "target-seeking ≈" in str(error.value)
        assert self.make(TWO, ROOMY).build() is not None

    def test_default_does_not_warn(self, warnings: list[str]) -> None:
        self.make((ANSWER_SKILL,)).build()
        assert warnings == []


class TestStartUp:
    """FR-19 «Закреплённый skill отсутствует в каталоге»: the service does not start."""

    @pytest.fixture
    def quiet_context(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        async def nothing() -> None:
            return None

        monkeypatch.setattr(api.APP_CTX, "on_startup", nothing)
        monkeypatch.setattr(api.APP_CTX, "on_shutdown", nothing)
        monkeypatch.setattr(factory, "build_model", lambda: ScriptedChatModel(steps=[]))
        factory.get_agent_v2.cache_clear()
        yield
        factory.get_agent_v2.cache_clear()

    async def test_unknown_name_stops_start(self, quiet_context: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setattr(APP_CONFIG.agent, "pinned_skills", ("no-such-skill",))
        with pytest.raises(SkillsError, match="no-such-skill"):
            async with api.lifespan(None):
                pass

    async def test_pins_larger_than_tail_stop_start(
        self, quiet_context: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setattr(APP_CONFIG.agent, "pinned_skills", TWO)
        monkeypatch.setattr(APP_CONFIG.agent, "summary_keep", 0.05)  # 1.5k tail of the default 30k budget
        with pytest.raises(SkillsError, match="не помещаются"):
            async with api.lifespan(None):
                pass


class TestTurns:
    """FR-19: pinned skills in context, after compression, opening their tools; an unpinned one waits for read_file."""

    async def test_two_pinned_once_per_session(self, tmp_path: Path) -> None:
        model = ScriptedChatModel(steps=[AIMessage("Ответ 0."), AIMessage("Ответ 1.")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(
            model,
            default_catalog(),
            store=store,
            upload_dir=str(tmp_path),
            checkpointer=InMemorySaver(),
            pinned_skills=TWO,
        ).build()
        for i in range(2):
            await agent.ainvoke(
                {"messages": [HumanMessage(f"Вопрос {i}")]},
                config={"configurable": {"thread_id": "u1:s1"}},
                context=RequestContext(user_id="u1", session_id="s1"),
            )
        assert sorted(pinned_names(model.calls[0]["messages"])) == sorted(TWO)
        assert sorted(pinned_names(model.calls[1]["messages"])) == sorted(TWO)  # second turn: no new copies
        assert not any(c.get("name") == "read_file" for call in model.calls for c in call.get("tool_calls", []))

    async def test_pinned_again_after_compression(self, tmp_path: Path, domain: list) -> None:
        """agent-loop AR-Q6: the compressing call goes without the cut pins; they are back before the next call."""
        model = SummarizingModel(steps=[*padding_reads(12), AIMessage("Готово.")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        session = Session(model, store, tmp_path, plan_mode="off", pinned_skills=TWO, budget=ROOMY)
        await session.ask("Прочитай справку")
        calls = compressed_calls(model)
        assert calls
        for call in model.calls:
            names = pinned_names(call["messages"])
            assert all(names.count(n) <= 1 for n in TWO)  # never a second copy
        after = model.calls[calls[0] + 1]["messages"]
        assert sorted(pinned_names(after)) == sorted(TWO)  # both back before the next call

    @pytest.mark.parametrize(("pinned", "status"), [((ANSWER_SKILL, "sensitivity-drivers"), "ok"), ((), "error")])
    async def test_pinned_skill_opens_its_tool(
        self, tmp_path: Path, inline_gate: object, pinned: tuple[str, ...], status: str
    ) -> None:
        store = await uploaded_store("u1", TRACE_INDEX, str(tmp_path))
        write_workbook(tmp_path / "user_file_u1.xlsx")
        steps = [tool_call("trace_dependencies", "c1", output_id="OUT1", year=2026), AIMessage("Готово.")]
        model = ScriptedChatModel(steps=steps)
        agent = AgentFactory(
            model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off", pinned_skills=pinned
        ).build()
        out = await agent.ainvoke(
            {"messages": [("user", "От чего зависит EBITDA 2026?")]},
            context=RequestContext(user_id="u1", session_id="s1"),
        )
        (name, result), *_ = tool_results(out)
        assert name == "trace_dependencies"
        assert result["status"] == status
        if pinned:
            assert "trace_dependencies" in model.calls[0]["tools"]
        else:
            assert result["code"] == "skill_required"

    async def test_unpinned_waits_for_read(self, tmp_path: Path) -> None:
        model = ScriptedChatModel(steps=[AIMessage("Ответ.")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(
            model, default_catalog(), store=store, upload_dir=str(tmp_path), pinned_skills=(ANSWER_SKILL,)
        ).build()
        await agent.ainvoke(
            {"messages": [("user", "Какой должна быть цена метанола, чтобы EBITDA 2026 = 1000?")]},
            context=RequestContext(user_id="u1", session_id="s1"),
        )
        messages = model.calls[0]["messages"]
        assert pinned_names(messages) == [ANSWER_SKILL]
        assert not any('<skill name="target-seeking"' in m.text for m in messages[1:])
        assert "/skills/target-seeking/SKILL.md" in messages[0].text  # still in the catalogue


class TestApiContract:
    """FR-19 «Контракт API не меняется»."""

    async def ask(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pinned: tuple[str, ...]) -> Any:
        tmp_path.mkdir()
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        model = ScriptedChatModel(steps=[AIMessage("Ответ по модели.")])
        return await Api(monkeypatch, v2(model, store, tmp_path, pinned_skills=pinned)).ask()

    async def test_same_response(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        default = await self.ask(monkeypatch, tmp_path / "a", (ANSWER_SKILL,))
        other = await self.ask(monkeypatch, tmp_path / "b", TWO)
        assert default.status_code == other.status_code == 200
        skip = {"date", "content-length", "x-response-time"}  # times differ; zip timestamps may differ by a second
        assert {k: v for k, v in default.headers.items() if k not in skip} == {
            k: v for k, v in other.headers.items() if k not in skip
        }
        names = [sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) for r in (default, other)]
        assert names[0] == names[1] == ["generated/agent_output.xlsx", "txt_response.txt"]

    def test_no_skill_field_in_request(self) -> None:
        schema = api.app_main.openapi()
        body = schema["paths"]["/api/v1/invoke-agent"]["post"]["requestBody"]
        assert "skill" not in json.dumps(body).lower()
        assert "skill" not in json.dumps(schema.get("components", {}).get("schemas", {})).lower()
