"""S3.2 (FR-12, FR-14): skill economic-answer, its pinning, answer structure and hygiene checks."""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_skills import write_skill

from aigw_service.api.v2.answer_check import SECTIONS, hygiene_issues, is_analytic, structure_issues
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import DEFAULT_PINNED, AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel
from aigw_service.api.v2.skills_check import SKILLS_DIR, SkillsError, validate_skills
from aigw_service.api.v2.tools.catalog import default_catalog

GOOD = """\
## Вывод
EBITDA 2026 растёт с ценой метанола: при 300 — 1 200,5, при 500 — 1 501. Риск для обслуживания долга умеренный.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| EBITDA при Метанол = 300 | 2026 | 1 200,5 | млн руб. |

## Анализ
Связь цены и EBITDA близка к линейной.

## Где в модели
Входы: «Метанол». Выходы: «EBITDA».

## Оговорки
Сетка с шагом 100.
"""


def known_tools() -> set[str]:
    """What ``AgentFactory.build`` validates skills against: base tools and skill tools (S3.4)."""
    catalog = default_catalog()
    return {t.name for t in [*catalog.base_tools(), *catalog.skill_tools()]}


class TestSkill:
    def test_shipped_and_valid(self) -> None:
        names = [s.name for s in validate_skills(SKILLS_DIR, known_tools())]
        assert "economic-answer" in names
        assert DEFAULT_PINNED == ("economic-answer",)

    def test_body_sets_sections_in_order(self) -> None:
        body = (SKILLS_DIR / "economic-answer" / "SKILL.md").read_text(encoding="utf-8")
        positions = [body.index(f"## {name}\n") for name in SECTIONS]
        assert positions == sorted(positions)
        assert "Без разделов" in body
        assert "а не вопрос пользователя" in body
        assert "Недостижимо" in body
        assert "никогда не пиши ID показателей" in body
        template = body[body.index("## Шаблон") :].split("```")[1]
        assert structure_issues(template) == []


class TestPinning:
    """FR-12 «Skill закреплён»."""

    @staticmethod
    async def session(tmp_path: Path, steps: int) -> tuple[ScriptedChatModel, list[dict]]:
        model = ScriptedChatModel(steps=[AIMessage(f"Ответ {i}.") for i in range(steps)])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(
            model, default_catalog(), store=store, upload_dir=str(tmp_path), checkpointer=InMemorySaver()
        ).build()
        outs = []
        for i, session in enumerate(["s1", "s1", "s2"][:steps]):
            outs.append(
                await agent.ainvoke(
                    {"messages": [HumanMessage(f"Вопрос {i}")]},
                    config={"configurable": {"thread_id": f"u1:{session}"}},
                    context=RequestContext(user_id="u1", session_id=session),
                )
            )
        return model, outs

    @staticmethod
    def pinned(messages: list) -> list:
        return [m for m in messages if m.additional_kwargs.get("lc_source") == "pinned_skill"]

    async def test_once_per_session(self, tmp_path: Path) -> None:
        model, outs = await self.session(tmp_path, 3)
        first = model.calls[0]["messages"]
        assert [m.additional_kwargs["skill"]["name"] for m in self.pinned(first)] == ["economic-answer"]
        assert self.pinned(first)[0].content.startswith('<skill name="economic-answer"')
        assert first[-1] is self.pinned(first)[0]  # the pin follows the question; the model still sees it
        assert len(self.pinned(model.calls[1]["messages"])) == 1  # same session: no second copy
        assert len(self.pinned(outs[2]["messages"])) == 1  # new session: its own copy
        assert outs[0]["turn_message_id"] == outs[0]["messages"][0].id  # the turn starts at the question

    def test_unknown_pinned_skill_stops_build(self, tmp_path: Path) -> None:
        skills = write_skill(tmp_path / "skills")
        with pytest.raises(SkillsError, match="economic-answer: закреплённый skill не найден"):
            AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), skills_dir=skills).build()


class TestStructure:
    """FR-12 «Проверка структуры»."""

    def test_good_answer(self) -> None:
        assert is_analytic(GOOD)
        assert structure_issues(GOOD) == []
        assert hygiene_issues(GOOD) == []

    def test_bold_headings_and_colons(self) -> None:
        text = "**Вывод:**\nВсё в порядке.\n\n**Ключевые цифры**\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        assert structure_issues(text) == []

    def test_empty_sections_may_be_omitted(self) -> None:
        assert structure_issues("## Вывод\nКоротко.\n\n## Оговорки\nДопущение.") == []

    @pytest.mark.parametrize(
        ("text", "issue"),
        [
            ("## Анализ\nТекст.", "нет раздела «Вывод»"),
            ("EBITDA растёт с ценой метанола.", "нет раздела «Вывод»"),
            ("## Анализ\nТекст.\n## Вывод\nКоротко.", "раздел «Вывод» не первый"),
            ("## Вывод\nОдно.\n## Оговорки\nДа.\n## Анализ\nНет.", "разделы не в порядке"),
            ("## Вывод\nОдно.\n## Анализ\nА.\n## Анализ\nБ.", "раздел «Анализ» повторяется"),
            ("## Вывод\nРаз. Два. Три. Четыре.", "«Вывод» длиннее 3 предложений"),
            ("## Вывод\nОдно.\n## Ключевые цифры\nEBITDA 1 200.", "нет таблицы"),
            ("## Вывод\nОдно.\n## Анализ\n\n## Оговорки\nДа.", "раздел «Анализ» пустой"),
        ],
    )
    def test_violation(self, text: str, issue: str) -> None:
        assert any(issue in i for i in structure_issues(text))

    def test_decimals_do_not_split_sentences(self) -> None:
        text = "## Вывод\nDSCR 1.25 при цене 300.5 выше порога 1.2. Запас есть. Риск низкий."
        assert structure_issues(text) == []

    def test_question_is_not_analytic(self) -> None:
        assert not is_analytic("За какой год посчитать сценарии?")


class TestHygiene:
    """FR-14 «Служебные данные в ответе»."""

    @pytest.mark.parametrize(
        ("text", "issue"),
        [
            ("Шаг не выполнен: missing_input.", "код ошибки missing_input"),
            ('Ошибка Traceback (most recent call last):\n  File "x.py", line 3', "traceback"),
            ("Значение показателя IN12 и OUT3 выросло.", "ID индекса: IN12, OUT3"),
            ("Я вызвал run_scenarios для расчёта.", "имя инструмента run_scenarios"),
            ("EBITDA grows with the methanol price in every scenario.", "ответ не на русском"),
        ],
    )
    def test_violation(self, text: str, issue: str) -> None:
        assert issue in hygiene_issues(text)

    def test_abbreviations_are_fine(self) -> None:
        assert hygiene_issues("DSCR и Debt/EBITDA по цене меди (LME) остаются выше ковенант.") == []

    def test_words_containing_codes_are_fine(self) -> None:
        assert hygiene_issues("Входы INFLATION и показатель INCOME не являются ID; отменено пользователем.") == []
