"""S3.1 (FR-18, AR-Q14, AR-Q15): skills catalogue, read_file only, skill tools, skills validation, start-up checks."""

import json
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import StoreBackend
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service import api
from aigw_service.api.v2 import factory
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.prompts.middleware import READ_FILE_DESCRIPTION, SKILLS_SYSTEM_PROMPT
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.skills_check import SKILLS_DIR, SkillInfo, SkillsError, validate_skills
from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog
from aigw_service.api.v2.tools.envelope import ToolSuccess
from aigw_service.config import APP_CONFIG
from aigw_service.core.tracing.langsmith_guard import forbid_langsmith

FORBIDDEN = {"write_file", "edit_file", "ls", "glob", "grep", "execute", "task"}


def skill_md(
    name: str = "demo-skill",
    description: str = "Демо-сценарий. Demo scenario.",
    tools: str | None = None,
    body: str = "",
) -> str:
    metadata = f"metadata:\n  include_tools: {tools}\n" if tools else ""
    return f"---\nname: {name}\ndescription: {description}\n{metadata}---\n\n# Демо\n\n1. Шаг плана.\n{body}"


def write_skill(root: Path, folder: str = "demo-skill", text: str | None = None) -> Path:
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / "SKILL.md").write_text(skill_md() if text is None else text, encoding="utf-8")
    return root


class DemoArgs(BaseModel):
    x: int


DEMO_TOOL = ToolSpec(
    name="demo_calc",
    description="Демо-инструмент skill.",
    args_schema=DemoArgs,
    call=lambda args, _ctx: ToolSuccess(result=f"demo {args.x}"),
    availability="skill",
)


def catalog_with_skill_tool() -> ToolCatalog:
    catalog = default_catalog()
    catalog.register(DEMO_TOOL)
    return catalog


async def run(
    tmp_path: Path, steps: list, skills: Path, catalog: ToolCatalog | None = None, user: str = "u1", store: Any = None
) -> tuple[dict, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    store = store or await uploaded_store(user, INDEX, str(tmp_path))
    agent = AgentFactory(
        model, catalog or default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off", skills_dir=skills
    ).build()
    out = await agent.ainvoke(
        {"messages": [("user", "Запрос")]}, context=RequestContext(user_id=user, session_id="s1")
    )
    return out, model


def tool_messages(out: dict) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


class TestValidation:
    """AR-Q15 «Невалидный skill останавливает старт»."""

    def test_valid_skill(self, tmp_path: Path) -> None:
        write_skill(tmp_path, text=skill_md(tools="demo_calc"))
        (tmp_path / "README.md").write_text("формат", encoding="utf-8")
        assert validate_skills(tmp_path, {"demo_calc"}) == [
            SkillInfo("demo-skill", "Демо-сценарий. Demo scenario.", ("demo_calc",))
        ]

    def test_empty_and_shipped_catalogues_are_valid(self, tmp_path: Path) -> None:
        assert validate_skills(tmp_path, set()) == []
        assert (SKILLS_DIR / "README.md").is_file()
        validate_skills(SKILLS_DIR, {t.name for t in default_catalog().base_tools()})

    @pytest.mark.parametrize(
        ("text", "problem"),
        [
            (None, "нет SKILL.md"),
            ("# без фронтматтера", "нет фронтматтера"),
            ("---\nname: [demo\n---\n", "битый YAML"),
            ("---\n- a\n- b\n---\n", "должен быть словарём"),
            (skill_md(description=""), "нет description"),
            ("---\ndescription: x\n---\n", "нет name"),
            (skill_md(name="Demo_Skill"), "строчные латинские буквы"),
            (skill_md(name="demo--skill"), "строчные латинские буквы"),
            (skill_md(name="other-skill"), "не совпадает с именем папки"),
            (skill_md(description="x" * 1025), "description длиннее 1024"),
            (skill_md(tools="unknown_tool"), "неизвестные инструменты в include_tools: unknown_tool"),
            (skill_md(tools="demo_calc, other"), "строка имён инструментов через пробел"),
            ("---\nname: demo-skill\ndescription: x\nmetadata: 5\n---\n", "metadata должна быть словарём"),
            (skill_md(body="строка\n" * 300), "тело длиннее 300 строк"),
        ],
    )
    def test_violation(self, tmp_path: Path, text: str | None, problem: str) -> None:
        if text is None:
            (tmp_path / "demo-skill").mkdir()
        else:
            write_skill(tmp_path, text=text)
        with pytest.raises(SkillsError, match=problem) as error:
            validate_skills(tmp_path, {"demo_calc"})
        assert error.value.problems[0].startswith("demo-skill: ")

    def test_all_violations_at_once(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "a-skill", skill_md(name="a-skill", description=""))
        write_skill(tmp_path, "b-skill", skill_md(name="wrong"))
        write_skill(tmp_path, "ok-skill", skill_md(name="ok-skill"))
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        with pytest.raises(SkillsError) as error:
            validate_skills(tmp_path, set())
        assert [p.split(":")[0] for p in error.value.problems] == ["a-skill", "b-skill", "notes.txt"]
        assert "Невалидные skills:\n- a-skill: нет description" in str(error.value)

    def test_missing_folder(self, tmp_path: Path) -> None:
        with pytest.raises(SkillsError, match="каталог skills не найден"):
            validate_skills(tmp_path / "nope", set())

    def test_build_stops_on_broken_skill(self, tmp_path: Path) -> None:
        write_skill(tmp_path, text="# без фронтматтера")
        with pytest.raises(SkillsError):
            AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), skills_dir=tmp_path).build()


class TestSkillsInAgent:
    """FR-18 «Декларативные skills»."""

    async def test_catalogue_in_prompt(self, tmp_path: Path) -> None:
        skills = write_skill(tmp_path / "skills")
        _, model = await run(tmp_path, [AIMessage("ок")], skills)
        system = model.calls[0]["messages"][0].text
        assert "## Skills — методики сложных расчётов" in system
        assert "demo-skill" in system
        assert "Демо-сценарий. Demo scenario." in system
        assert "/skills/demo-skill/SKILL.md" in system
        assert "Skills System" not in system  # the built-in English prompt is replaced

    async def test_new_skill_without_code_changes(self, tmp_path: Path) -> None:
        skills = write_skill(tmp_path / "skills")
        _, before = await run(tmp_path, [AIMessage("ок")], skills)
        write_skill(skills, "second-skill", skill_md(name="second-skill", description="Второй сценарий."))
        out, after = await run(
            tmp_path,
            [tool_call("read_file", "r1", file_path="/skills/second-skill/SKILL.md", limit=400), AIMessage("ок")],
            skills,
        )
        assert "second-skill" not in before.calls[0]["messages"][0].text
        assert "/skills/second-skill/SKILL.md" in after.calls[0]["messages"][0].text
        assert "Второй сценарий." in tool_messages(out)[0].content

    async def test_skill_opens_its_tool(self, tmp_path: Path) -> None:
        skills = write_skill(tmp_path / "skills", text=skill_md(tools="demo_calc"))
        out, model = await run(
            tmp_path,
            [
                tool_call("read_file", "r1", file_path="/skills/demo-skill/SKILL.md"),
                tool_call("demo_calc", "c1", x=2),
                AIMessage("ок"),
            ],
            skills,
            catalog_with_skill_tool(),
        )
        assert "demo_calc" not in model.calls[0]["tools"]
        assert "demo_calc" in model.calls[1]["tools"]
        assert json.loads(tool_messages(out)[1].content)["result"] == "demo 2"

    @pytest.mark.parametrize(
        "path", ["/etc/passwd", "/skills/../skills_check.py", "/skills/../../../../.env", "/other/file.md"]
    )
    async def test_only_own_paths(self, tmp_path: Path, path: str) -> None:
        skills = write_skill(tmp_path / "skills")
        (tmp_path / "skills_check.py").write_text("SECRET = 1", encoding="utf-8")
        out, _ = await run(tmp_path, [tool_call("read_file", "r1", file_path=path), AIMessage("ок")], skills)
        message = tool_messages(out)[0]
        assert message.status == "error"
        assert "not found" in message.content or "Path traversal not allowed" in message.content
        assert "SECRET" not in message.content

    async def test_results_of_another_user_are_not_found(self, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        for owner in ("u1", "u2"):
            StoreBackend(namespace=lambda _rt, o=owner: ("results", o), store=store).write(
                f"/t1/{owner}.json", json.dumps({"owner": owner})
            )
        steps = [
            tool_call("read_file", "r1", file_path="/results/t1/u1.json"),
            tool_call("read_file", "r2", file_path="/results/t1/u2.json"),
            AIMessage("ок"),
        ]
        out, _ = await run(tmp_path, steps, write_skill(tmp_path / "skills"), store=store)
        own, other = tool_messages(out)
        assert own.status == "success"
        assert '"owner": "u1"' in own.content
        assert other.status == "error"
        assert other.content == "Error: File '/t1/u2.json' not found"


class TestNoWriteOrExecute:
    """AR-Q14 «Нет записи файлов и исполнения кода»."""

    async def test_tool_list(self, tmp_path: Path) -> None:
        _, model = await run(tmp_path, [AIMessage("ок")], write_skill(tmp_path / "skills"))
        tools = set(model.calls[0]["tools"])
        assert "read_file" in tools
        assert not tools & FORBIDDEN

    async def test_write_file_is_rejected(self, tmp_path: Path) -> None:
        skills = write_skill(tmp_path / "skills")
        out, _ = await run(
            tmp_path, [tool_call("write_file", "w1", file_path="/skills/x.md", content="x"), AIMessage("ок")], skills
        )
        message = tool_messages(out)[0]
        assert message.status == "error"
        assert "not a valid tool" in message.content
        assert not (skills / "x.md").exists()

    def test_prompts_are_russian(self) -> None:
        assert "Доступные skills" in SKILLS_SYSTEM_PROMPT
        assert all(
            slot in SKILLS_SYSTEM_PROMPT for slot in ("{skills_locations}", "{skills_load_warnings}", "{skills_list}")
        )
        assert "/skills/" in READ_FILE_DESCRIPTION
        assert "/results/" in READ_FILE_DESCRIPTION

    @pytest.mark.parametrize(
        ("env", "stops"),
        [
            ({"LANGSMITH_TRACING": "true"}, True),
            ({"LANGCHAIN_TRACING_V2": "TRUE"}, True),
            ({"LANGSMITH_TRACING": "1"}, True),
            ({"LANGSMITH_TRACING": "false"}, False),
            ({}, False),
        ],
    )
    def test_langsmith_forbidden(self, env: dict, stops: bool) -> None:
        if stops:
            with pytest.raises(RuntimeError, match="LangSmith запрещён"):
                forbid_langsmith(env)
        else:
            forbid_langsmith(env)


class TestStartUp:
    """AR-Q15: start-up builds agent v2, so a broken skill stops it; LangSmith stops both engines."""

    @pytest.fixture
    def quiet_context(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []

        async def startup() -> None:
            calls.append("startup")

        async def shutdown() -> None:
            calls.append("shutdown")

        monkeypatch.setattr(api.APP_CTX, "on_startup", startup)
        monkeypatch.setattr(api.APP_CTX, "on_shutdown", shutdown)
        monkeypatch.setattr(factory, "build_model", lambda: ScriptedChatModel(steps=[]))
        factory.get_agent_v2.cache_clear()
        yield calls
        factory.get_agent_v2.cache_clear()

    async def start(self) -> None:
        async with api.lifespan(None):
            pass

    async def test_v2_with_broken_skill_stops(
        self, quiet_context: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        write_skill(tmp_path, text="# без фронтматтера")
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setattr(factory, "SKILLS_DIR", tmp_path)
        with pytest.raises(SkillsError):
            await self.start()

    async def test_v2_builds_on_start(self, quiet_context: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        await self.start()
        assert factory.get_agent_v2.cache_info().currsize == 1
        assert quiet_context == ["startup", "shutdown"]

    async def test_v1_does_not_build_v2(self, quiet_context: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        await self.start()
        assert factory.get_agent_v2.cache_info().currsize == 0

    async def test_langsmith_stops_any_engine(self, quiet_context: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        with pytest.raises(RuntimeError, match="LangSmith"):
            await self.start()
        assert quiet_context == []
