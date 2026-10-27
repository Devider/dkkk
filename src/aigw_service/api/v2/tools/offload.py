"""Offload of long tool results to ``/results/`` (ADR-7, AR-Q6, AR-Q7; spec §9 правило 3, §10 «Контракт» п. 9).

A successful envelope whose JSON is longer than the limit is saved in full to the user's store and the model gets a
compact envelope: summary, notes, hint, as many ``numbers`` as fit, and ``ref`` — the path ``read_file`` reads.
The namespace comes from the request's ``user_id``, never from the path, so nobody reads another user's results.
"""

import json
import re
from typing import Any

from deepagents.backends import StoreBackend

from aigw_service.api.v2.tools.envelope import ToolSuccess, to_json
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

RESULTS_ROUTE = "/results/"
NOT_SAVED_NOTE = "Полный результат не сохранён: используй сводку и числа выше."
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def results_namespace(user_id: str) -> tuple[str, ...]:
    """Store namespace of one user's results: the same for writing (adapter) and reading (``read_file``)."""
    return ("results", user_id)


def result_path(turn_id: str | None, call_id: str | None) -> str:
    """``/results/<turn_id>/<call_id>.json`` with path-unsafe characters replaced."""
    turn = _UNSAFE.sub("_", turn_id or "") or "turn"
    call = _UNSAFE.sub("_", call_id or "") or "call"
    return f"{RESULTS_ROUTE}{turn}/{call}.json"


def _store_key(path: str) -> str:
    """Key inside the ``/results/`` route: ``CompositeBackend`` strips the route prefix and keeps a leading slash."""
    return "/" + path.removeprefix(RESULTS_ROUTE)


def _truncate(text: str, size: int) -> str:
    if size <= 0:
        return ""
    return text if len(text) <= size else text[: size - 1] + "…"


def compact(envelope: ToolSuccess, ref: str | None, limit: int) -> ToolSuccess:
    """Envelope without ``data`` whose JSON is not longer than ``limit`` (design D2).

    ``numbers`` are cut from the end first (a note says how many are shown), then the summary is truncated.
    """
    notes = list(envelope.notes)
    if ref is None:
        notes.append(NOT_SAVED_NOTE)
    small = ToolSuccess(result=envelope.result, numbers=envelope.numbers, notes=notes, hint=envelope.hint, ref=ref)
    if len(to_json(small)) <= limit:
        return small
    total = len(envelope.numbers)

    def with_numbers(count: int) -> ToolSuccess:
        cut_note = f"Показаны первые {count} из {total} чисел; полный список — в ref." if ref else None
        return small.model_copy(
            update={"numbers": envelope.numbers[:count], "notes": [*notes, *([cut_note] if cut_note else [])]}
        )

    low, high = 0, total  # largest prefix of numbers that fits
    while low < high:
        middle = (low + high + 1) // 2
        if len(to_json(with_numbers(middle))) <= limit:
            low = middle
        else:
            high = middle - 1
    fitted = with_numbers(low)
    if len(to_json(fitted)) > limit:  # even without numbers: shorten the summary to the room left
        room = limit - len(to_json(fitted.model_copy(update={"result": ""})))
        fitted = fitted.model_copy(update={"result": _truncate(fitted.result, room)})
    if len(to_json(fitted)) > limit:  # very long notes or hint: keep only what points to the full result
        fitted = ToolSuccess(result=_truncate(envelope.result, limit // 4), ref=ref)
    return fitted


async def offload(
    envelope: ToolSuccess,
    *,
    store: Any,
    user_id: str,
    turn_id: str | None,
    call_id: str | None,
    limit: int,
) -> ToolSuccess:
    """``envelope`` unchanged when it fits ``limit``; otherwise saved to ``/results/`` and returned compact."""
    if len(to_json(envelope)) <= limit:
        return envelope
    path = result_path(turn_id, call_id)
    namespace = results_namespace(user_id)
    content = json.dumps(envelope.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=1)
    ref: str | None = path
    try:
        if store is None:
            raise RuntimeError("no store in the tool runtime")
        backend = StoreBackend(store=store, namespace=lambda _runtime: namespace)
        written = await backend.awrite(_store_key(path), content)
        if getattr(written, "error", None):
            raise RuntimeError(written.error)
    except Exception as exc:  # pylint: disable=broad-exception-caught  # design D5: the result is still useful
        logger.opt(exception=True).error("Result offload to {} failed: {}", path, str(exc))
        ref = None
    else:
        logger.info("Result offloaded to {} [{}]: {} chars", path, user_id, len(content))
    return compact(envelope, ref, limit)
