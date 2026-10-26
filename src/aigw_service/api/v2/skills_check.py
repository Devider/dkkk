"""Skills validation at agent build and service start (AR-Q15, spike S0.3 K8, spec §11 «Структура»).

deepagents' ``SkillsMiddleware`` silently skips a broken skill (only a log warning), truncates a long description and
only warns about a bad name. These rules are a strict superset: whatever deepagents would skip or fix quietly is an
error here, and every violation of every skill is reported at once.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILL_FILE = "SKILL.md"
ROOT_FILES = {"README.md"}
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 300

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)  # the same pattern deepagents parses
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    include_tools: tuple[str, ...] = ()


class SkillsError(RuntimeError):
    """One or more skills are invalid; the message lists every violation as ``<folder>: <problem>``."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Невалидные skills:\n" + "\n".join(f"- {p}" for p in problems))


def validate_skills(root: Path, known_tools: set[str]) -> list[SkillInfo]:
    """Skills of ``root`` sorted by name, or ``SkillsError`` with all violations."""
    problems: list[str] = []
    skills: list[SkillInfo] = []
    if not root.is_dir():
        raise SkillsError([f"{root}: каталог skills не найден"])
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_file():
            if entry.name not in ROOT_FILES:
                problems.append(f"{entry.name}: посторонний файл в корне каталога skills")
            continue
        found, errors = _check_skill(entry, known_tools)
        problems.extend(f"{entry.name}: {e}" for e in errors)
        if found:
            skills.append(found)
    if problems:
        raise SkillsError(problems)
    return skills


def _check_skill(folder: Path, known_tools: set[str]) -> tuple[SkillInfo | None, list[str]]:
    path = folder / SKILL_FILE
    if not path.is_file():
        return None, [f"нет {SKILL_FILE}"]
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        return None, ["нет фронтматтера YAML между строками ---"]
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, [f"битый YAML во фронтматтере: {str(exc).splitlines()[0]}"]
    if not isinstance(meta, dict):
        return None, ["фронтматтер должен быть словарём"]

    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    errors = _name_errors(name, folder.name) + _description_errors(description)
    tools, tool_errors = _include_tools(meta.get("metadata"), known_tools)
    errors.extend(tool_errors)
    body_lines = len(text[match.end() :].splitlines())
    if body_lines > MAX_BODY_LINES:
        errors.append(f"тело длиннее {MAX_BODY_LINES} строк ({body_lines})")
    return (None if errors else SkillInfo(name, description, tools)), errors


def _name_errors(name: str, folder: str) -> list[str]:
    if not name:
        return ["нет name"]
    if len(name) > MAX_NAME or not _NAME.match(name):
        return [f"name {name!r}: строчные латинские буквы, цифры и одиночные «-», до {MAX_NAME} символов"]
    if name != folder:
        return [f"name {name!r} не совпадает с именем папки"]
    return []


def _description_errors(description: str) -> list[str]:
    if not description:
        return ["нет description"]
    if len(description) > MAX_DESCRIPTION:
        return [f"description длиннее {MAX_DESCRIPTION} символов ({len(description)})"]
    return []


def _include_tools(metadata: object, known_tools: set[str]) -> tuple[tuple[str, ...], list[str]]:
    if metadata is None:
        return (), []
    if not isinstance(metadata, dict):
        return (), ["metadata должна быть словарём"]
    raw = metadata.get("include_tools")
    if raw is None:
        return (), []
    if not isinstance(raw, str) or "," in raw:
        return (), ["metadata.include_tools — строка имён инструментов через пробел"]
    tools = tuple(raw.split())
    unknown = [t for t in tools if t not in known_tools]
    return tools, [f"неизвестные инструменты в include_tools: {', '.join(unknown)}"] if unknown else []
