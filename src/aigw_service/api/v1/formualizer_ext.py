"""Custom function patches for the `formualizer` backend.

formualizer (a Rust/calamine engine) natively covers the functions used by
real-world financial models on **0.8.1**; a few gaps remain and are closed by
workbook-local overrides (via
``Workbook.register_function(..., allow_override_builtin=True)``), ported from
the xl_libx_benchmark project:

* HYPERLINK   — not implemented by the engine (#NAME?) -> return friendly name
* SHEET       — native `_xlfn.SHEET` (how Excel/openpyxl store it) returns 2
                for a single-sheet workbook (0.8.1) -> constant 1
* TODAY       — engine's deterministic clock returns 1970 epoch -> fixed date
* CELL        — not implemented; custom functions receive VALUES only, so
                CELL("contents", ref) returns the cell value (info types
                needing cell coordinates are impossible)
* XNPV / XIRR — built-ins return #NUM! on date-typed inputs (dates are dropped
                during collection, values/dates length mismatch) -> own impls

TRANSPOSE is native-correct since 0.8.1 and needs no patch.

Notes:
* Callbacks receive arguments BY VALUE: a single-cell reference arrives as its
  value, a range arrives as a nested list of rows (column -> [[v],[v],...]).
* Dates arrive as datetime.date objects; convert to Excel serial for results.
"""

import datetime

import formualizer as fz

_SER0 = datetime.date(1899, 12, 30)

# Fixed date used for TODAY (Excel serial 45500 = 2024-07-27). The engine's
# built-in TODAY() returns the deterministic epoch (serial 25569 = 1970-01-01)
# when no clock is injected.
_TODAY_SERIAL = 45500


def _column(m):
    """Extract a 1-D list from a column range shape [[v],[v],...]."""
    return [row[0] if isinstance(row, (list, tuple)) else row for row in m]


def _to_serial(x):
    if isinstance(x, datetime.date):
        return (x - _SER0).days
    if isinstance(x, datetime.datetime):
        return (x.date() - _SER0).days
    return float(x)


def _hyperlink(link, friendly_name=None):
    return friendly_name if friendly_name is not None else link


def _cell(info_type, reference=None):
    # Only "contents" is testable: a custom function sees the reference's
    # VALUE, never its coordinates, so "col"/"row"/"address" are impossible.
    return reference


def _sheet(*_args):
    return 1


def _today():
    return _SER0 + datetime.timedelta(days=_TODAY_SERIAL)


def _xnpv(rate, values, dates):
    v = [float(x) for x in _column(values)]
    d = [_to_serial(x) for x in _column(dates)]
    d0 = d[0]
    return sum(v[i] / (1.0 + rate) ** ((d[i] - d0) / 365.0) for i in range(len(v)))


def _xirr(values, dates):
    lo, hi = -0.999999, 1000.0

    def f(r):
        return _xnpv(r, values, dates)

    for _ in range(300):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


PATCHES = {
    "HYPERLINK": (_hyperlink, 1, 2),
    "CELL": (_cell, 1, 2),
    "SHEET": (_sheet, 0, 1),
    # Excel/openpyxl store SHEET with the `_xlfn.` prefix; the engine resolves
    # such names through its native table, bypassing the plain-name override,
    # so the same callback is registered under the prefixed alias too
    "_XLFN.SHEET": (_sheet, 0, 1),
    "TODAY": (_today, 0, 0),
    "XNPV": (_xnpv, 3, 3),
    "XIRR": (_xirr, 2, 2),
}


def register_patches(wb):
    """Register all patches on a loaded workbook (in-place)."""
    for name, (fn, lo, hi) in PATCHES.items():
        wb.register_function(name, fn, min_args=lo, max_args=hi, allow_override_builtin=True)


def to_native(v):
    """Normalize an evaluated cell to a plain Python value (or error string)."""
    if isinstance(v, fz.ExcelError):  # pylint: disable=no-member
        return f"ERROR: {v.kind}"
    if isinstance(v, dict) and isinstance(v.get("kind"), str) and v.get("type") == "Error":
        return f"ERROR: {v['kind']}"
    if isinstance(v, datetime.date):
        return float(_to_serial(v))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v
