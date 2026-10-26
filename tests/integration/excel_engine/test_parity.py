"""S0.7: excel_engine gives the same results as the v1 calculations on the Methanex workbook (needs LibreOffice)."""

import json
import random
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("uno", reason="LibreOffice (python3-uno) is required; run inside the Docker image")

import excel_engine.calculations as v2
from aigw_service.api.v1 import tools as v1

WORKBOOK = str(Path(__file__).resolve().parents[3] / "models" / "Methanex_FinModel.xlsx")
TIMESTAMP = re.compile(r"\d{8}_\d{6}")
DURATION = re.compile(r'("\w*seconds\w*": )[\d.]+')
MESSAGE_DURATION = re.compile(r"(за )[\d.]+( сек)")

EMA_ARGS = {
    "file_name": WORKBOOK,
    "input_names": ["Метанол"],
    "output_names": ["EBITDA", "Чистая прибыль"],
    "output_years": [2026, 2026],
    "ranges": [[300, 500]],
    "steps": [100],
}
IFT_ARGS = {
    "file_name": WORKBOOK,
    "output_name": "EBITDA",
    "output_year": 2026,
    "target_value": 1000,  # reachable: EBITDA 2026 is ~714 at 300 and ~1075 at 400 USD/t
    "input_names": ["Метанол"],
    "max_scenarios": 30,
}


def _run(module: Any, func: str, args: dict) -> str:
    """Call with a clean cache and a fixed seed; drop timestamps and elapsed times."""
    module._ANALYSIS_CACHE.clear()  # pylint: disable=protected-access
    module._ANALYSIS_CACHE_ORDER.clear()  # pylint: disable=protected-access
    random.seed(0)
    result = getattr(module, func)(**args)
    dumped = json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True, default=str)
    dumped = DURATION.sub(r"\1<elapsed>", TIMESTAMP.sub("<ts>", dumped))
    return MESSAGE_DURATION.sub(r"\1<elapsed>\2", dumped)


@pytest.mark.parametrize(
    ("func", "args"),
    [("analyze_excel_model", EMA_ARGS), ("analyze_model_inputs_for_target", IFT_ARGS)],
    ids=["ema", "ift"],
)
def test_copy_matches_v1(func: str, args: dict) -> None:
    expected = _run(v1, func, args)
    actual = _run(v2, func, args)
    assert '"status": "OK"' in expected, f"v1 call did not succeed, parity is meaningless: {expected[:300]}"
    assert actual == expected
