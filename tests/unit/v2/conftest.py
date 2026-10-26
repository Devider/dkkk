"""Unit tests never start a real LibreOffice pool: calculation tools get an in-process gate."""

import pytest
from tests.unit.v2.fakes import InlineGate

from aigw_service.api.v2.tools import calc_tools


@pytest.fixture(autouse=True)
def inline_gate(monkeypatch: pytest.MonkeyPatch) -> InlineGate:
    gate = InlineGate()
    monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
    return gate
