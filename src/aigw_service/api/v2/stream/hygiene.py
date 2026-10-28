"""FR-24 check of one stream event (S6.2 design D8): what must never reach the frontend.

Used by the tests of every stream scenario; S6.5 may log its findings. The ``answer`` text is exempt: it must
equal ``txt_response.txt`` of the same turn (FR-21, FR-26).
"""

import re
from collections.abc import Iterator
from typing import Any

from aigw_service.api.v2.stream.events import AnswerEvent, StreamEvent
from aigw_service.api.v2.stream.labels import ID_PATTERN, error_codes, tool_names
from aigw_service.api.v2.tools.envelope import ErrorCode

SERVICE_PATTERNS = {
    "results path": re.compile(r"/results/"),
    "traceback": re.compile(r"Traceback|File \"[^\"]+\", line \d+"),
    "exception": re.compile(r"\b\w*(?:Exception|Error)\b"),
    "envelope key": re.compile(r"\"(?:code|hint|error)\"\s*:"),
}


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}[{i}]")


def violations(event: StreamEvent) -> list[str]:
    """``"<field>: <what>"`` for every forbidden fragment in the event's text fields."""
    data = event.data()
    if isinstance(event, AnswerEvent):
        data.pop("text")
    data.pop("trace_id", None)
    data.pop("ts", None)
    words = tool_names() | error_codes() | {ErrorCode.CANCELLED.value}
    found: list[str] = []
    for path, text in _strings(data, event.event):
        if ID_PATTERN.search(text):
            found.append(f"{path}: index ID")
        found += [f"{path}: {word}" for word in re.findall(r"[a-z_]+", text) if word in words]
        found += [f"{path}: {name}" for name, pattern in SERVICE_PATTERNS.items() if pattern.search(text)]
    return found
