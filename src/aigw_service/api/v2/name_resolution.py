"""Workbook catalogs and the V2 name-resolution subagent."""

import json
import os
import re
import tempfile
from typing import Annotated, Literal, TypedDict

import openpyxl
from deepagents.middleware.subagents import CompiledSubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from aigw_service.api.v1.tools import get_store_file, normalize_text
from aigw_service.api.v2.catalog_generations import get_repository
from aigw_service.api.v2.prompts.name_resolution import (
    EXTRACT_NAMES_PROMPT,
    INPUT_RESOLUTION_PROMPT,
    OUTPUT_RESOLUTION_PROMPT,
)


class CatalogEntry(TypedDict):
    """One named row from an Inputs or Outputs sheet."""

    id: str
    name: str


class Catalogs(TypedDict):
    """Input and output catalog snapshot for one workbook."""

    inputs: list[CatalogEntry]
    outputs: list[CatalogEntry]


class NameResolutionItem(TypedDict):
    """One requested name and its validated catalog resolution."""

    mentioned_name: str
    resolved_name: str | None
    status: Literal["resolved", "unresolved"]
    reason: str


class NameResolutionResult(TypedDict):
    """Canonical input and output names returned by the resolver."""

    inputs: list[NameResolutionItem]
    outputs: list[NameResolutionItem]


class ExtractedNames(BaseModel):
    """Names explicitly requested as workbook inputs and outputs."""

    inputs: list[str] = Field(default_factory=list, description="Все явно упомянутые входные параметры")
    outputs: list[str] = Field(default_factory=list, description="Все явно упомянутые выходные показатели")


class InputItem(BaseModel):
    """
    Упомянутый пользователем входной параметр и его эквивалент из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS)
    """

    mentioned_input_name: str = Field(description="Входной параметр, упомянутый пользователем")
    explanation: str = Field(description="Объяснение, почему выбран именно этот эквивалент")
    equivalent_input_name: str = Field(
        description=(
            "Эквивалент входного параметра из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS). "
            "Запиши так как дано в СТАНДАРТНОМ СПИСКЕ, не переводи, не расшифровывай"
        ),
    )
    equivalent_input_id: str = Field(
        description="Идентификатор эквивалента входного параметра из СТАНДАРТНОГО СПИСКА в формате INid"
    )
    is_unambiguous: bool = Field(
        description="True, если найден ровно один обоснованный эквивалент c высокой долей уверенности"
    )
    candidate_names: list[str] = Field(
        description="Если 'is_unambiguous' равно False, то укажи список канонических названий из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS),"
        "которые могут быть эквивалентами упомянутого пользователем входного параметра"
    )


class InputResolutionAnalysis(BaseModel):
    """
    Список упомянутых пользователем входных параметров и их эквивалентов из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS)
    """

    mentioned_inputs: list[InputItem] = Field(
        description="Список ВСЕХ входных параметров, упомянутых пользователем с найденными эквивалентами из СТАНДАРТНОГО СПИСКА ВХОДНЫХ ПАРАМЕТРОВ (INPUTS)"
    )


class OutputItem(BaseModel):
    """
    Упомянутый пользователем выходной показатель
    """

    mentioned_output_name: str = Field(description="Выходной показатель, упомянутый пользователем")
    explanation: str = Field(description="Краткое объяснение, как ты выбираешь эквивалент")
    equivalent_output_id: str = Field(default="", description="ID строки OUT из кандидатов этого упоминания")
    equivalent_output_name: str = Field(
        description=(
            "Эквивалент выходного показателя из СТАНДАРТНОГО СПИСКА ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS). "
            "Запиши так как дано в СТАНДАРТНОМ СПИСКЕ, не переводи, не расшифровывай"
        ),
    )
    is_unambiguous: bool = Field(
        description="True, если найден ровно один обоснованный эквивалент c высокой долей уверенности"
    )
    candidate_names: list[str] = Field(
        description="Если 'is_unambiguous' равно False, то укажи список канонических названий из"
        "СТАНДАРТНОГО СПИСКА ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS), которые могут быть эквивалентами упомянутого пользователем выходного показателя"
    )


class OutputResolutionAnalysis(BaseModel):
    """
    Список упомянутых пользователем выходных показателей и их эквивалентов из СТАНДАРТНОГО СПИСКА ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS)
    """

    mentioned_outputs: list[OutputItem] = Field(
        description="Список ВСЕХ выходных показателей, упомянутых пользователем с найденными эквивалентами из СТАНДАРТНОГО СПИСКА ВЫХОДНЫХ ПОКАЗАТЕЛЕЙ (OUTPUTS)"
    )


class _ResolverState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    catalogs: Catalogs
    mentions: ExtractedNames
    input_matches: list[dict]
    output_matches: list[dict]
    pending: list[dict]
    result: NameResolutionResult
    generation: str
    original_request: str
    restart_request: str | None
    candidates: dict[str, list[list[CatalogEntry]]]


def build_workbook_catalog(path: str) -> Catalogs:
    """Read named rows and one-based Excel row IDs without calculating formulas."""
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        result: Catalogs = {"inputs": [], "outputs": []}
        for sheet_name, kind, catalog_key in (
            ("Inputs", "IN", "inputs"),
            ("Outputs", "OUT", "outputs"),
        ):
            if sheet_name not in workbook.sheetnames:
                continue
            sheet = workbook[sheet_name]
            headers = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
            try:
                name_column = headers.index("Наименование")
            except ValueError:
                continue
            for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
                if name_column >= len(row) or row[name_column] is None:
                    continue
                name = str(row[name_column]).strip()
                if name:
                    result[catalog_key].append({"id": f"{kind}:{row_number}", "name": name})
        return result
    finally:
        workbook.close()


def get_user_workbook_catalog(user_id: str | None) -> Catalogs:
    """Load the current user's uploaded workbook catalog from trusted identity."""
    if not user_id:
        raise ValueError("Не удалось определить пользователя для поиска загруженной модели.")
    filename = get_store_file(user_id)
    if not filename:
        raise FileNotFoundError("Сначала загрузите Excel-модель через /api/v2/upload.")
    path = os.path.join(tempfile.gettempdir(), os.path.basename(filename))
    if not os.path.isfile(path):
        raise FileNotFoundError("Загруженная Excel-модель не найдена. Загрузите её повторно.")
    return build_workbook_catalog(path)


def _extractor_node(state: _ResolverState, *, llm: BaseChatModel) -> dict[str, object]:
    """Extract requested parameter and metric names from the delegated task."""
    request_text = state.get("restart_request") or "\n".join(
        str(message.content) for message in state.get("messages", []) if hasattr(message, "content")
    )
    extractor = llm.with_structured_output(ExtractedNames)
    mentions = extractor.invoke(
        [
            SystemMessage(content=EXTRACT_NAMES_PROMPT),
            HumanMessage(content=request_text),
        ]
    )
    return {"mentions": mentions, "original_request": request_text, "restart_request": None}


def _resolve_catalog(
    mentions: list[str],
    catalog: list[CatalogEntry],
    response_schema: type[InputResolutionAnalysis] | type[OutputResolutionAnalysis],
    response_field: Literal["mentioned_inputs", "mentioned_outputs"],
    prompt_template: str,
    *,
    llm: BaseChatModel,
    candidate_lists: list[list[CatalogEntry]] | None = None,
) -> list[dict[str, object]]:
    """Adapt InputItem/OutputItem responses to validated catalog entry IDs."""
    if not mentions:
        return []
    candidate_lists = candidate_lists if candidate_lists is not None else [catalog for _ in mentions]
    matcher = llm.with_structured_output(response_schema)
    catalog_text = "\n\n".join(
        f"Упоминание {index}: {mention}\n" + "\n".join(f"{entry['id']} | {entry['name']}" for entry in rows)
        for index, (mention, rows) in enumerate(zip(mentions, candidate_lists, strict=True))
    )
    prompt = prompt_template.format(catalog=catalog_text or "(каталог пуст)")
    mentioned_text = "\n".join(f"{index}. {mention}" for index, mention in enumerate(mentions))
    selection = matcher.invoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=f"Сопоставь каждое упоминание из списка:\n{mentioned_text}"),
        ]
    )
    response_items = getattr(selection, response_field)
    mention_field = "mentioned_input_name" if response_field == "mentioned_inputs" else "mentioned_output_name"
    name_field = "equivalent_input_name" if response_field == "mentioned_inputs" else "equivalent_output_name"
    results: list[dict[str, object]] = []
    for index, mention in enumerate(mentions):
        matched_items = [
            item for item in response_items if normalize_text(getattr(item, mention_field)) == normalize_text(mention)
        ]
        if not matched_items and len(response_items) == len(mentions):
            matched_items = [response_items[index]]
        candidate_ids = _candidate_ids_from_items(matched_items, candidate_lists[index], response_field, name_field)
        is_unambiguous = bool(
            len(candidate_ids) == 1 and matched_items and all(item.is_unambiguous for item in matched_items)
        )
        results.append(
            {
                "mentioned_name": mention,
                "candidate_ids": candidate_ids,
                "is_unambiguous": is_unambiguous and len(candidate_ids) == 1,
                "reason": "; ".join(dict.fromkeys(item.explanation for item in matched_items))
                if matched_items
                else "Подходящий аналог в каталоге не найден.",
            }
        )
    return results


def _candidate_ids_from_items(
    items: list[InputItem] | list[OutputItem],
    catalog: list[CatalogEntry],
    response_field: Literal["mentioned_inputs", "mentioned_outputs"],
    name_field: str,
) -> list[str]:
    """Resolve model-proposed canonical names and IDs against the real catalog."""
    candidates: list[str] = []
    by_id = {entry["id"]: entry for entry in catalog}
    normalized_ids = {re.sub(r"[^a-zA-Z0-9]", "", entry_id).casefold(): entry_id for entry_id in by_id}
    id_field = "equivalent_input_id" if response_field == "mentioned_inputs" else "equivalent_output_id"
    for item in items:
        proposed_id = getattr(item, id_field, "")
        selected_id = normalized_ids.get(re.sub(r"[^a-zA-Z0-9]", "", proposed_id).casefold())
        if proposed_id and not selected_id:
            continue
        if selected_id and item.is_unambiguous:
            candidates.append(selected_id)
            continue
        if selected_id:
            candidates.append(selected_id)
        names = [*item.candidate_names, getattr(item, name_field, "")]
        candidates.extend(entry["id"] for entry in catalog if entry["name"] in names)
    return list(dict.fromkeys(candidates))


def _resolve_inputs_node(state: _ResolverState, *, llm: BaseChatModel) -> dict[str, object]:
    """Resolve each input mention only against its retrieved candidate list."""
    return {
        "input_matches": _resolve_catalog(
            state["mentions"].inputs,
            [],
            InputResolutionAnalysis,
            "mentioned_inputs",
            INPUT_RESOLUTION_PROMPT,
            llm=llm,
            candidate_lists=state["candidates"]["inputs"],
        )
    }


def _resolve_outputs_node(state: _ResolverState, *, llm: BaseChatModel) -> dict[str, object]:
    """Resolve each output mention only against its retrieved candidate list."""
    return {
        "output_matches": _resolve_catalog(
            state["mentions"].outputs,
            [],
            OutputResolutionAnalysis,
            "mentioned_outputs",
            OUTPUT_RESOLUTION_PROMPT,
            llm=llm,
            candidate_lists=state["candidates"]["outputs"],
        )
    }


def _choices(state: _ResolverState) -> list[dict[str, object]]:
    """Collect zero or multiple candidate matches across both catalogs."""
    pending: list[dict[str, object]] = []
    for kind, matches, catalog in (
        ("inputs", state.get("input_matches", []), state["catalogs"]["inputs"]),
        ("outputs", state.get("output_matches", []), state["catalogs"]["outputs"]),
    ):
        entries = {entry["id"]: entry for entry in catalog}
        pending.extend(
            {
                "kind": kind,
                "match_index": match_index,
                "mentioned_name": match["mentioned_name"],
                "candidate_ids": match["candidate_ids"],
                "candidates": [entries[candidate_id] for candidate_id in match["candidate_ids"]],
            }
            for match_index, match in enumerate(matches)
            if match["candidate_ids"] and (not match["is_unambiguous"] or len(match["candidate_ids"]) != 1)
        )
    return pending


def _parse_selection(answer: str, choice_count: int) -> list[int] | None:
    """Parse a complete comma-separated list of numeric option selections."""
    raw = [part.strip() for part in answer.split(",")]
    if len(raw) != choice_count or any(not re.fullmatch(r"\d+", part) for part in raw):
        return None
    selections = [int(part) for part in raw]
    if any(number < 1 for number in selections):
        return None
    return selections


def _clarify_node(state: _ResolverState) -> dict[str, object]:
    """Interrupt for all unresolved choices and validate the numeric reply."""
    pending = _choices(state)
    if not pending:
        return {"pending": []}
    prompt_lines = ["Помогите уточнить названия, которые не удалось определить однозначно:"]
    for index, item in enumerate(pending, start=1):
        prompt_lines.append(
            f"{index}. Не удалось однозначно сопоставить «{item['mentioned_name']}». Возможно, вы имеете в виду:"
        )
        if not item["candidates"]:
            prompt_lines.append("   Подходящие варианты в каталоге не найдены.")
        for candidate_number, candidate in enumerate(item["candidates"], start=1):
            prompt_lines.append(f"   {candidate_number}) {candidate['name']}")
        prompt_lines.append(f"   {len(item['candidates']) + 1}) Ни один из перечисленных вариантов")
    prompt_lines.append("Выберите номер для каждого названия и отправьте ответы через запятую, например: 1, 2.")
    while True:
        answer = interrupt({"question": "\n".join(prompt_lines), "pending": pending})
        if not isinstance(answer, str):
            answer = str(answer)
        selections = _parse_selection(answer, len(pending))
        if selections is None or any(
            selected > len(item["candidates"]) + 1 for selected, item in zip(selections, pending, strict=True)
        ):
            prompt_lines.append("Некорректный или неполный ответ. Введите допустимые номера через запятую.")
            continue
        for selected, item in zip(selections, pending, strict=True):
            if selected <= len(item["candidates"]):
                item["selected_id"] = item["candidates"][selected - 1]["id"]
            else:
                item["selected_id"] = None
        return {"pending": pending}


def _finalize_node(state: _ResolverState) -> dict[str, object]:
    """Validate IDs and return canonical names with no catalog identifiers."""
    selected = {(item["kind"], item["match_index"]): item.get("selected_id") for item in state.get("pending", [])}
    result: NameResolutionResult = {"inputs": [], "outputs": []}
    for kind, matches, catalog in (
        ("inputs", state.get("input_matches", []), state["catalogs"]["inputs"]),
        ("outputs", state.get("output_matches", []), state["catalogs"]["outputs"]),
    ):
        entries = {entry["id"]: entry["name"] for entry in catalog}
        for match_index, match in enumerate(matches):
            candidate_ids = match["candidate_ids"]
            selected_id = selected.get((kind, match_index))
            was_selected_by_user = selected_id is not None
            candidate_id = candidate_ids[0] if match["is_unambiguous"] and len(candidate_ids) == 1 else selected_id
            canonical_name = entries.get(candidate_id) if candidate_id in entries else None
            reason = (
                f"Пользователь выбрал вариант «{canonical_name}». Используем его для дальнейших расчётов."
                if canonical_name and was_selected_by_user
                else match["reason"]
                if canonical_name
                else (
                    f"Для «{match['mentioned_name']}» подходящий аналог в каталоге не найден."
                    if not candidate_ids
                    else "Пользователь отклонил предложенные варианты."
                )
            )
            result[kind].append(
                {
                    "mentioned_name": match["mentioned_name"],
                    "resolved_name": canonical_name,
                    "status": "resolved" if canonical_name else "unresolved",
                    "reason": reason,
                }
            )
    return {"result": result, "messages": [AIMessage(content=json.dumps(result, ensure_ascii=False))]}


def build_name_resolver(llm: BaseChatModel) -> CompiledSubAgent:
    """Build the typed parallel resolver exposed to the V2 Deep Agent."""
    workflow = StateGraph(_ResolverState)

    def extract(state: _ResolverState) -> dict[str, object]:
        """Extract mentions using the configured language model."""
        return _extractor_node(state, llm=llm)

    def resolve_inputs(state: _ResolverState) -> dict[str, object]:
        """Resolve input mentions using the configured language model."""
        return _resolve_inputs_node(state, llm=llm)

    def resolve_outputs(state: _ResolverState) -> dict[str, object]:
        """Resolve output mentions using the configured language model."""
        return _resolve_outputs_node(state, llm=llm)

    workflow.add_node("extract", extract)
    workflow.add_node("resolve_inputs", resolve_inputs)
    workflow.add_node("resolve_outputs", resolve_outputs)

    def clarify(state: _ResolverState, config: RunnableConfig) -> Command:
        """Discard superseded choices before consuming a numeric resume answer."""
        repo = get_repository()
        user_id = config.get("configurable", {}).get("user_id")
        with repo.lock(user_id):
            generation = repo.load(user_id)
        if generation.identity != state["generation"]:
            # Consume this interrupt's obsolete resume value without applying it.
            interrupt({"question": "Модель обновлена. Повторно сопоставляем исходный запрос."})
            return Command(
                goto="load_catalogs",
                update={
                    "restart_request": state["original_request"],
                    "pending": [],
                    "input_matches": [],
                    "output_matches": [],
                },
            )
        return Command(goto="finalize", update=_clarify_node(state))

    workflow.add_node("clarify", clarify)
    workflow.add_node("finalize", _finalize_node)

    def load_catalogs(state: _ResolverState, config: RunnableConfig) -> dict[str, object]:
        """Load catalogs for the authenticated user in the inherited runtime config."""
        user_id = config.get("configurable", {}).get("user_id")
        repo = get_repository()
        with repo.lock(user_id):
            generation = repo.load(user_id)
        return {"catalogs": generation.index.catalogs, "generation": generation.identity}

    def retrieve(state: _ResolverState, config: RunnableConfig) -> dict[str, object]:
        """Embed mentions in category batches and checkpoint JSON candidate snapshots."""
        repo = get_repository()
        user_id = config.get("configurable", {}).get("user_id")
        with repo.lock(user_id):
            generation = repo.load(user_id)
            if generation.identity != state["generation"]:
                raise ValueError("Workbook changed during resolution; retry the original request")
            candidates = {
                kind: generation.index.candidates(
                    kind, getattr(state["mentions"], kind), repo.embeddings, repo.settings.retrieval_k
                )
                for kind in ("inputs", "outputs")
            }
        return {"candidates": candidates}

    workflow.add_node("load_catalogs", load_catalogs)
    workflow.add_edge(START, "load_catalogs")
    workflow.add_edge("load_catalogs", "extract")
    workflow.add_node("retrieve", retrieve)
    workflow.add_edge("extract", "retrieve")
    workflow.add_edge("retrieve", "resolve_inputs")
    workflow.add_edge("retrieve", "resolve_outputs")
    workflow.add_edge(["resolve_inputs", "resolve_outputs"], "clarify")
    workflow.add_edge("finalize", END)
    graph = workflow.compile(checkpointer=None)
    return CompiledSubAgent(
        name="workbook-name-resolver",
        description="Разрешает упомянутые пользователем Inputs и Outputs по каталогу загруженной модели до расчётов.",
        runnable=graph,
    )
