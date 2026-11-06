"""LibreOffice Calc backend via UNO (pyuno bridge).

Replaces ``formualizer`` with the real LibreOffice Calc formula engine.
Every ``ExcelWorkbook`` spawns its own headless ``soffice`` on a free port
with a throwaway user profile, opens the workbook there, and evaluates
everything through the Calc engine -- no Python-side formula reimplementation.

Key facts (verified on python:3.12-slim + Debian python3-uno):
  * pyuno needs ``URE_BOOTSTRAP`` pointing at LibreOffice's ``fundamentalrc``
    for the bridge to work; without it the URP bridge silently dies with
    "Binary URP bridge disposed during call".
  * ``--accept`` must NOT carry a ``StarOffice.ServiceManager`` suffix;
    connect to ``StarOffice.ComponentContext`` instead.
  * The Desktop is created from the *remote* context:
    ``ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)``

Documents are normally opened ReadOnly and never written back.  The one
exception is the upload correction flow: ``CalcBook.save_as_xlsx`` stores the
document through ``storeToURL`` (filter ``Calc MS Excel 2007 XML``), which
preserves the Calc-computed cached values.  Because the source is ReadOnly,
the output path must differ from the source path (the caller replaces the
source afterwards).  ``CalcBook.fix_long_choose`` rewrites ``CHOOSE`` formulas
with more than 30 arguments (LibreOffice's limit; Excel allows 254) to the
equivalent ``INDEX`` form when their value arguments form a contiguous
single-row range.
"""

import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional

import loguru

logger = loguru.logger

DEFAULT_MEMORY_LIMIT = "3G"
USED_AREA_WARN_CELLS = 20_000_000

# Suffix -> multiplier to MB. A bare number is treated as MB; an explicit
# 'b' suffix means bytes (docker-style).
_MEM_UNIT_MB = {"k": 1 / 1024, "m": 1.0, "g": 1024.0}
_BYTES_TO_MB = 1 / (1024 * 1024)
_RE_MEMORY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)(i?)([bB]?)\s*$")


def parse_memory_limit(raw: str) -> float:
    """Parse a docker-style memory value (``100m``, ``3G``, ``512k``) to MB.

    Suffixes are case-insensitive; optional ``i`` endings accepted (``MiB``).
    An explicit ``b`` suffix means bytes, a bare number means MB.
    """
    m = _RE_MEMORY.match(str(raw))
    if not m:
        raise ValueError(f"unsupported memory value {raw!r} (use e.g. '100m', '3G')")
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit:
        return value * _MEM_UNIT_MB[unit]
    if m.group(4):
        return value * _BYTES_TO_MB
    return value


def get_memory_limit_mb(env_var: str = "LO_MEMORY_LIMIT") -> float:
    """RSS limit for soffice from the environment; invalid values fall back."""
    raw = os.environ.get(env_var)
    if not raw:
        return parse_memory_limit(DEFAULT_MEMORY_LIMIT)
    try:
        return parse_memory_limit(raw)
    except ValueError:
        logger.warning(
            "{}={!r} is invalid ({}) — falling back to {}MB",
            env_var,
            raw,
            parse_memory_limit.__name__ + " failed",
            DEFAULT_MEMORY_LIMIT,
        )
        return parse_memory_limit(DEFAULT_MEMORY_LIMIT)


def _proc_rss_mb(pid: Optional[int]) -> Optional[float]:
    """Current ``VmRSS`` of *pid* in MB, or ``None`` if unreadable."""
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


class SofficeRssWatch:
    """Kill ``soffice`` if its RSS exceeds the configured limit.

    Bridge calls like a whole-sheet read can drive ``soffice`` into multiple
    GB while never returning; an unwatched process ends up killed by the OOM
    killer taking the whole service down.  The watch samples ``VmRSS`` in a
    daemon thread; on breach it SIGKILLs the process, so the blocked bridge
    call raises and the caller can translate it into a controlled error via
    :meth:`error`.

    One watch spans the whole soffice session lifetime (armed in
    :meth:`LibreOfficeSession.start`, disarmed in :meth:`LibreOfficeSession.stop`),
    not individual operations.
    """

    def __init__(
        self,
        session: "LibreOfficeSession",
        label: str = "",
        limit_mb: Optional[float] = None,
        interval: float = 0.5,
        logger=None,
    ):
        self.session = session
        self.label = label
        self.limit_mb = get_memory_limit_mb() if limit_mb is None else limit_mb
        self.interval = interval
        self.log = logger or loguru.logger
        self.current_sheet: Optional[str] = None
        self.peak_mb = 0.0
        self.exceeded_mb = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"soffice-rss-watch[{label}]", daemon=True)
        self._started = False

    def _run(self):
        proc = self.session.proc
        pid = proc.pid if proc is not None else None
        while not self._stop.wait(self.interval):
            rss = _proc_rss_mb(pid)
            if rss is None:
                continue
            self.peak_mb = max(self.peak_mb, rss)
            if rss <= self.limit_mb:
                continue
            self.exceeded_mb = rss
            self.log.error(
                "soffice RSS {:.0f}MB exceeded LO_MEMORY_LIMIT={:.0f}MB during {}: sheet={} — killing pid {}",
                rss,
                self.limit_mb,
                self.label,
                self.current_sheet,
                pid,
            )
            try:
                if proc is not None:
                    proc.kill()
            except Exception:
                pass
            return

    @property
    def triggered(self) -> bool:
        return self.exceeded_mb > 0

    def error(self) -> Optional[RuntimeError]:
        if not self.triggered:
            return None
        return RuntimeError(
            f"soffice RSS {self.exceeded_mb:.0f}MB exceeded LO_MEMORY_LIMIT="
            f"{self.limit_mb:.0f}MB during {self.label}"
            + (f" (sheet={self.current_sheet})" if self.current_sheet else "")
        )

    def start(self):
        """Arm the sampler thread (idempotent)."""
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self):
        """Disarm the sampler thread (idempotent, safe even before start)."""
        if not self._started:
            return
        self._started = False
        self._stop.set()
        self._thread.join(timeout=2)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def _raise_if_rss_killed(watch: "SofficeRssWatch", exc: BaseException):
    if watch is None:
        return
    err = watch.error()
    if err is not None:
        raise err from exc


LO_PROGRAM_CANDIDATES = (
    "/usr/lib64/libreoffice/program",
    "/usr/lib/libreoffice/program",
    "/usr/lib/python3/dist-packages",
)

UNO = None
_ENUMS: dict = {}

# ---------------------------------------------------------------------------
# CHOOSE -> INDEX helpers (fix_long_choose)
# ---------------------------------------------------------------------------

# A simple cell reference: optional $ on column and row, letters + digits.
_RE_CELL_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def _col_letter(col: int) -> str:
    """1-based column number -> column letters (1 -> A, 27 -> AA)."""
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _col_number(letters: str) -> int:
    """Column letters -> 1-based column number (A -> 1, AA -> 27)."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _split_args(inner: str) -> Optional[list]:
    """Split a formula argument list on top-level ``;`` / ``,``.

    Respects nested parentheses, double-quoted string literals (with ``""``
    escapes) and ``'sheet'`` quoting.  Returns ``None`` if a quote or paren is
    unbalanced.
    """
    args: list[str] = []
    depth = 0
    buf: list[str] = []
    in_str = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_str:
            buf.append(ch)
            if ch == '"':
                if i + 1 < len(inner) and inner[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_str = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
            buf.append(ch)
        elif depth == 0 and ch in ";,":
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if in_str or depth != 0:
        return None
    args.append("".join(buf).strip())
    return args


def _looks_like_long_choose(formula: str, max_args: int) -> bool:
    """True if *formula* is a ``CHOOSE`` call with more than *max_args* args.

    Matches ``CHOOSE`` case-insensitively anywhere in the formula (Calc
    formulas start with ``=``).
    """
    for m in re.finditer(r"(?<![A-Za-z0-9_.])CHOOSE\s*\(", formula, re.IGNORECASE):
        start = m.end() - 1  # at '('
        depth = 0
        i = start
        while i < len(formula):
            ch = formula[i]
            if ch == '"':
                i += 1
                while i < len(formula):
                    if formula[i] == '"':
                        if i + 1 < len(formula) and formula[i + 1] == '"':
                            i += 2
                            continue
                        break
                    i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            continue
        inner = formula[start + 1 : i]
        args = _split_args(inner)
        if args is not None and len(args) > max_args + 1:
            # args[0] is the index; value args are args[1:]
            return True
    return False


def _choose_to_index(formula: str) -> tuple[Optional[str], Optional[str]]:
    """Rewrite a long ``CHOOSE`` to ``INDEX`` if its value args are contiguous.

    Returns ``(new_formula, None)`` on success or ``(None, reason)`` when the
    formula cannot be safely rewritten.

    The rewrite requires every value argument to be a plain cell reference
    (``$?COL$?ROW``) on the **same row** with **contiguous** column numbers and
    no absolute/relative mixing that would break drag-fill: the absolute-ness
    of column and row is taken from the first argument and must be uniform
    across all arguments.  The result is ``INDEX(<range>; <index>)`` with the
    same absolute-ness on the range endpoints and the original index expression.
    """
    m = re.search(r"(?<![A-Za-z0-9_.])CHOOSE\s*\(", formula, re.IGNORECASE)
    if m is None:
        return None, "choose_not_found"
    start = m.end() - 1
    depth = 0
    i = start
    while i < len(formula):
        ch = formula[i]
        if ch == '"':
            i += 1
            while i < len(formula) and formula[i] != '"':
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None, "unbalanced_choose"

    inner = formula[start + 1 : i]
    args = _split_args(inner)
    if args is None or len(args) < 2:
        return None, "unparsable_args"

    index_expr = args[0]
    value_args = args[1:]

    refs = []
    for a in value_args:
        a_clean = a.strip()
        rm = _RE_CELL_REF.match(a_clean)
        if not rm:
            return None, f"non_reference_arg:{a_clean[:20]}"
        col_abs = a_clean.startswith("$")
        rest = a_clean[1:] if col_abs else a_clean
        row_abs = "$" in rest
        col = _col_number(rm.group(1))
        row = int(rm.group(2))
        refs.append((col, row, col_abs, row_abs))

    row0 = refs[0][1]
    col_abs0 = refs[0][2]
    row_abs0 = refs[0][3]
    if any(r != row0 for _, r, _, _ in refs):
        return None, "multiple_rows"
    if any(ca != col_abs0 for _, _, ca, _ in refs):
        return None, "mixed_column_abs"
    if any(ra != row_abs0 for _, _, _, ra in refs):
        return None, "mixed_row_abs"
    cols = [c for c, _, _, _ in refs]
    if cols != list(range(cols[0], cols[0] + len(cols))):
        return None, "non_contiguous_columns"

    col_prefix = "$" if col_abs0 else ""
    row_prefix = "$" if row_abs0 else ""
    first = f"{col_prefix}{_col_letter(cols[0])}{row_prefix}{row0}"
    last = f"{col_prefix}{_col_letter(cols[-1])}{row_prefix}{row0}"
    prefix = formula[: m.start()]
    suffix = formula[i + 1 :]
    new_formula = f"{prefix}INDEX({first}:{last};{index_expr}){suffix}"
    return new_formula, None


def _candidate_program_dirs(explicit: Optional[str] = None) -> list:
    dirs = []
    if explicit:
        dirs.append(explicit)
    env = os.environ.get("LO_PROGRAM_DIR")
    if env:
        dirs.append(env)
    dirs.extend(LO_PROGRAM_CANDIDATES)
    uniq = []
    for d in dirs:
        d = os.path.abspath(d)
        if os.path.isdir(d) and d not in uniq:
            uniq.append(d)
    return uniq


def _set_ure_bootstrap() -> None:
    """Make sure ``URE_BOOTSTRAP`` points at LibreOffice's ``fundamentalrc``.

    Must run *before* ``import uno``: pyuno reads the variable at import time
    and a missing/empty value produces a bridge that dies with
    "Binary URP bridge disposed during call". Called at module import so any
    later ``import uno`` (direct or via pyoo) sees a valid bootstrap.
    """
    if os.environ.get("URE_BOOTSTRAP"):
        return
    for d in _candidate_program_dirs():
        fund = os.path.join(d, "fundamentalrc")
        if os.path.exists(fund):
            os.environ["URE_BOOTSTRAP"] = "vnd.sun.star.pathname:" + fund
            return


_set_ure_bootstrap()


def _try_import_uno():
    try:
        import uno
    except ImportError:
        return None
    if not (hasattr(uno, "getComponentContext") and hasattr(uno, "systemPathToFileUrl")):
        return None
    # Warm up unohelper while the search path that provides it is still in
    # place: pyoo/UNO resolve it lazily, after ensure_uno has restored sys.path.
    try:
        import unohelper  # noqa: F401
    except ImportError:
        pass
    return uno


def ensure_uno(lo_program_dir: Optional[str] = None):
    """Import and expose the uno module, extending sys.path if needed.

    ``URE_BOOTSTRAP`` must point at LibreOffice's ``fundamentalrc`` even when
    ``uno`` is already importable (e.g. via system dist-packages) — without it
    the URP bridge dies with "Binary URP bridge disposed during call".

    ``/usr/lib/python3/dist-packages`` is added to ``sys.path`` only for the
    duration of ``import uno``/``unohelper`` and then removed. Leaving it in
    place permanently shadows the venv's modern ``typing_extensions`` with the
    (older) system copy, which breaks ``pydantic_core``'s ``Sentinel`` import in
    the ``spawn`` workers of the LO process pool (they re-import ``__main__``
    from a clean interpreter). ``pyuno.so`` and ``com.sun.star.*`` do not rely
    on ``sys.path``: pyuno is located via ``URE_BOOTSTRAP`` and the UNO import
    hook installed by ``uno.py``.
    """
    global UNO
    if UNO is not None:
        return UNO
    tried = []
    for d in _candidate_program_dirs(lo_program_dir):
        tried.append(d)
        if os.path.exists(os.path.join(d, "fundamentalrc")):
            os.environ.setdefault(
                "URE_BOOTSTRAP",
                "vnd.sun.star.pathname:" + os.path.join(d, "fundamentalrc"),
            )
            break
    UNO = _try_import_uno()
    if UNO is not None:
        return UNO
    for d in _candidate_program_dirs(lo_program_dir):
        tried.append(d)
        if not os.path.exists(os.path.join(d, "uno.py")):
            continue
        added = d not in sys.path
        if added:
            sys.path.insert(0, d)
        try:
            UNO = _try_import_uno()
        finally:
            # Scoped path injection: never leave the system dist-packages in
            # sys.path, or its older typing_extensions shadows the venv copy.
            if added:
                while d in sys.path:
                    sys.path.remove(d)
        if UNO is not None:
            return UNO
    raise RuntimeError(
        "PyUNO bindings not available (probed: %s). Fix with one of:\n"
        "  - Debian/Ubuntu: sudo apt install python3-uno\n"
        "  - pass --lo-program-dir / set LO_PROGRAM_DIR to LibreOffice's "
        "'program' directory containing uno.py + fundamentalrc "
        "(e.g. /usr/lib/libreoffice/program, /usr/lib64/libreoffice/program)" % (", ".join(tried) or "no extra dirs")
    )


def _prop(name: str, value: Any):
    p = UNO.createUnoStruct("com.sun.star.beans.PropertyValue")
    p.Name = name
    p.Value = value
    return p


def _enum(type_name: str, member: str):
    key = (type_name, member)
    if key not in _ENUMS:
        _ENUMS[key] = UNO.Enum(type_name, member)
    return _ENUMS[key]


class LibreOfficeSession:
    """Owns a headless soffice process speaking UNO on a private socket."""

    REUSE_TIMEOUT = 1.5
    START_TIMEOUT = 60.0

    def __init__(
        self,
        port: Optional[int] = None,
        lo_program_dir: Optional[str] = None,
        logger=None,
    ):
        ensure_uno(lo_program_dir)
        self.lo_program_dir = lo_program_dir
        self.port = port or self._free_port()
        self.ctx = None
        self.desktop = None
        self.proc: Optional[subprocess.Popen] = None
        self.profile_dir = None
        self.rss_watch: Optional[SofficeRssWatch] = None
        self.log = logger or loguru.logger
        self.soffice = self._find_soffice()

    @staticmethod
    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _find_soffice(self) -> str:
        cand = [os.path.join(d, "soffice") for d in _candidate_program_dirs(self.lo_program_dir)]
        cand += ["soffice", "libreoffice"]
        for c in cand:
            if os.path.basename(c) == c:
                found = shutil.which(c)
            elif os.path.isfile(c) and os.access(c, os.X_OK):
                found = c
            else:
                found = None
            if found:
                return found
        raise RuntimeError("soffice executable not found (searched PATH and LibreOffice program dirs)")

    def _connect(self, timeout: float):
        local = UNO.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        url = f"uno:socket,host=127.0.0.1,port={self.port};urp;StarOffice.ComponentContext"
        deadline = time.monotonic() + timeout
        last = None
        while True:
            try:
                self.ctx = resolver.resolve(url)
                break
            except Exception as exc:
                last = exc
                if self.proc is not None and self.proc.poll() is not None:
                    raise RuntimeError(
                        f"soffice exited with code {self.proc.returncode} (port {self.port} busy?)"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"cannot connect to soffice on port {self.port}: {last}") from exc
                time.sleep(0.25)
        smgr = self.ctx.ServiceManager
        self.desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)

    def start(self):
        self.profile_dir = tempfile.mkdtemp(prefix="aigw_lo_")
        profile_url = UNO.systemPathToFileUrl(self.profile_dir)
        cmd = [
            self.soffice,
            "--headless",
            "--invisible",
            "--norestore",
            "--nodefault",
            "--nologo",
            f"-env:UserInstallation={profile_url}",
            f"--accept=socket,host=127.0.0.1,port={self.port};urp;",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.rss_watch = SofficeRssWatch(self, label="soffice-session", logger=self.log)
        self.rss_watch.start()
        try:
            self._connect(self.START_TIMEOUT)
        except Exception:
            self.stop()
            raise

    def open_document(self, path: str):
        url = UNO.systemPathToFileUrl(os.path.abspath(path))
        try:
            doc = self.desktop.loadComponentFromURL(url, "_blank", 0, (_prop("Hidden", True), _prop("ReadOnly", True)))
        except Exception as e:
            _raise_if_rss_killed(self.rss_watch, e)
            raise
        if doc is None:
            raise RuntimeError(f"LibreOffice failed to open {path}")
        return doc

    def stop(self):
        if self.rss_watch is not None:
            watch = self.rss_watch
            self.rss_watch = None
            watch.stop()
            self.log.info(
                "LO session stopped: peak_soffice_mb={:.0f}, limit_mb={:.0f}",
                watch.peak_mb,
                watch.limit_mb,
            )
        if self.desktop is not None and self.proc is not None:
            try:
                self.desktop.terminate()
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
        self.proc = None
        self.desktop = None
        self.ctx = None
        if self.profile_dir:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


class CalcBook:
    """Thin wrapper over a remote spreadsheet document."""

    def __init__(self, session: LibreOfficeSession, path: str, logger=None):
        self.session = session
        self.path = path
        self.log = logger or loguru.logger
        self.doc = session.open_document(path)
        self._sheets: dict = {}
        self._names: Optional[list] = None

    @property
    def sheet_names(self) -> list:
        if self._names is None:
            sheets = self.doc.getSheets()
            self._names = [sheets.getByIndex(i).getName() for i in range(sheets.getCount())]
        return list(self._names)

    def sheet(self, name: str):
        if name not in self._sheets:
            self._sheets[name] = self.doc.Sheets.getByName(name)
        return self._sheets[name]

    def calculate(self):
        """Incremental recalculation of only the "dirty" cells whose inputs
        changed — mirrors Excel/desktop LibreOffice behaviour.

        Cells not reachable from a changed input are left "clean" and keep
        their cached values, so targets on heavy models (e.g. a ``CHOOSE``
        with >30 args outside the changed chain) still compute correctly.
        """
        self.doc.calculate()

    def calculate_all(self):
        """Force a full recalculation of the whole book (`doc.calculateAll()`).

        Unlike :meth:`calculate`, this touches every formula including
        heavy/unreadable cells (can cascade ``#VALUE!`` on ``CHOOSE``>30).
        Intended for explicit, debug-only use — not the scenario path.
        """
        self.doc.calculateAll()

    def set_value(self, sheet_name: str, row: int, col: int, value: Any):
        if hasattr(value, "item"):
            value = value.item()
        self.sheet(sheet_name).getCellByPosition(col - 1, row - 1).setValue(float(value))

    def set_formula(self, sheet_name: str, row: int, col: int, formula: str):
        self.sheet(sheet_name).getCellByPosition(col - 1, row - 1).setFormula(str(formula))

    def get_value(self, sheet_name: str, row: int, col: int) -> Any:
        return self._scalar(self.sheet(sheet_name).getCellByPosition(col - 1, row - 1))

    def read_block(self, sheet_name: str, r1: int, c1: int, r2: int, c2: int) -> list:
        """Bulk values of rows r1..r2, cols c1..c2 (1-based).

        Returns a list of rows; each row is a list with floats for numbers,
        strings for text/errors and ``None`` for empty cells.
        """
        sh = self.sheet(sheet_name)
        rng = sh.getCellRangeByPosition(c1 - 1, r1 - 1, c2 - 1, r2 - 1)
        data = rng.getDataArray()
        return [[self._normalize(v) for v in row] for row in data]

    @staticmethod
    def _normalize(v: Any) -> Any:
        """``getDataArray`` returns '' for empty cells -> None, keep the rest."""
        if v == "":
            return None
        return v

    @staticmethod
    def _scalar(cell) -> Any:
        """Value of one cell: float | str | None. Error results become strings."""
        t = cell.getType()
        if t == _enum("com.sun.star.table.CellContentType", "EMPTY"):
            return None
        if t == _enum("com.sun.star.table.CellContentType", "TEXT"):
            return cell.getString()
        if t == _enum("com.sun.star.table.CellContentType", "FORMULA"):
            if cell.getError():
                return cell.getString()
            v = cell.getValue()
            if math.isnan(v):
                return cell.getString()
            return v
        return cell.getValue()

    def close(self):
        try:
            self.doc.close(False)
        except Exception:
            try:
                self.doc.dispose()
            except Exception:
                pass

    def save_as_xlsx(self, path: str):
        """Store the document to *path* as an ``.xlsx`` (OOXML) file.

        Uses ``storeToURL`` with the ``Calc MS Excel 2007 XML`` filter, which
        preserves the Calc-computed values (including cached values of cells
        left untouched) and any formulas rewritten via :meth:`set_formula`.
        Unlike ``store()`` it writes to an explicit URL without changing the
        document's own "modified" path association.  Because the source
        document is opened ReadOnly, *path* must differ from the source path
        (the caller replaces the source afterwards).
        """
        target = os.path.abspath(path)
        url = UNO.systemPathToFileUrl(target)
        args = (
            _prop("FilterName", "Calc MS Excel 2007 XML"),
            _prop("Overwrite", True),
            _prop("Hidden", True),
        )
        watch = self.session.rss_watch
        try:
            self.doc.storeToURL(url, args)
        except Exception as e:
            _raise_if_rss_killed(watch, e)
            raise
        self.log.info(
            "LO save_as_xlsx done: target={}, peak_soffice_mb={}",
            target,
            watch.peak_mb if watch is not None else None,
        )

    # ------------------------------------------------------------------
    # CHOOSE -> INDEX normalisation (fix_long_choose)
    # ------------------------------------------------------------------

    MAX_CHOOSE_ARGS = 30

    def fix_long_choose(self, max_args: int = MAX_CHOOSE_ARGS) -> dict:
        """Replace ``CHOOSE`` formulas with >*max_args* args by ``INDEX``.

        LibreOffice Calc supports at most 30 ``CHOOSE`` arguments (Excel: 254);
        longer ones evaluate to ``Err:504`` and, once an input in their chain is
        perturbed, they break the incremental recalc of dependent outputs.  A
        ``CHOOSE(idx, a1..aN)`` whose value arguments form a contiguous
        single-row range is equivalent to ``INDEX(range, idx)`` and can be
        rewritten so Calc can evaluate it.

        Cells whose arguments are not a contiguous single-row range (column
        gap, several rows, cross-sheet, formula argument) are left unchanged and
        reported in ``unfixable`` with a reason.

        Shared formulas (``<f t="shared" si="…"/>`` slave cells, whose
        expression lives in the master cell) are handled via the cell's own
        formula, which Calc materialises on read.

        Traversal is sparse: instead of materialising the whole used-area
        rectangle (which explodes on books whose used area is inflated to
        millions of rows/columns by stray formatting or far-corner junk), only
        ranges containing actual formulas are queried via ``queryContentCells``
        and read block-wise.  Hidden sheets are skipped per business rules and
        reported in ``skipped_hidden``.

        Returns ``{"fixed", "unfixable": [{"cell", "formula", "reason"}],
        "scanned", "skipped_hidden", "formulas_scanned", "choose_seen"}``.
        """
        # CellFlags is a *constants group* (not an enum): FORMULA == 16.
        flags = UNO.getConstantByName("com.sun.star.sheet.CellFlags.FORMULA")
        fixed = 0
        unfixable: list[dict] = []
        scanned = 0
        skipped_hidden = 0
        formulas_scanned = 0
        choose_seen = 0
        watch = self.session.rss_watch
        started = time.monotonic()
        try:
            for sheet_name in self.sheet_names:
                watch.current_sheet = sheet_name
                sheet_started = time.monotonic()
                sh = self.sheet(sheet_name)
                if not sh.IsVisible:
                    skipped_hidden += 1
                    self.log.info("fix_long_choose: sheet={} hidden, skipped", sheet_name)
                    continue
                cur = sh.createCursor()
                cur.gotoEndOfUsedArea(False)
                addr = cur.RangeAddress
                if addr.EndRow < 0 or addr.EndColumn < 0:
                    self.log.info("fix_long_choose: sheet={} empty, skipped", sheet_name)
                    continue
                used_cells = (addr.EndRow + 1) * (addr.EndColumn + 1)
                if used_cells > USED_AREA_WARN_CELLS:
                    self.log.warning(
                        "fix_long_choose: sheet={} used area {}x{} ({} cells) >> content — relying on sparse scan",
                        sheet_name,
                        addr.EndColumn + 1,
                        addr.EndRow + 1,
                        used_cells,
                    )
                ranges = sh.getCellRangeByPosition(0, 0, addr.EndColumn, addr.EndRow).queryContentCells(flags)
                blocks = list(ranges.getRangeAddresses())
                sheet_formulas = 0
                sheet_choose = 0
                sheet_scanned = 0
                sheet_fixed = 0
                for block_addr in blocks:
                    block = sh.getCellRangeByPosition(
                        block_addr.StartColumn,
                        block_addr.StartRow,
                        block_addr.EndColumn,
                        block_addr.EndRow,
                    ).getFormulaArray()
                    for r_off, row in enumerate(block):
                        row_abs = block_addr.StartRow + r_off
                        for c_off, formula in enumerate(row):
                            if not formula or not isinstance(formula, str):
                                continue
                            formulas_scanned += 1
                            sheet_formulas += 1
                            if re.search(r"(?<![A-Za-z0-9_.])CHOOSE\s*\(", formula, re.IGNORECASE):
                                choose_seen += 1
                                sheet_choose += 1
                            if not _looks_like_long_choose(formula, max_args):
                                continue
                            scanned += 1
                            sheet_scanned += 1
                            col_abs = block_addr.StartColumn + c_off
                            cell_ref = f"{_col_letter(col_abs + 1)}{row_abs + 1}"
                            replacement, reason = _choose_to_index(formula)
                            if replacement is not None:
                                self.set_formula(sheet_name, row_abs + 1, col_abs + 1, replacement)
                                fixed += 1
                                sheet_fixed += 1
                            else:
                                unfixable.append(
                                    {"cell": f"{sheet_name}!{cell_ref}", "formula": formula, "reason": reason}
                                )
                self.log.info(
                    "fix_long_choose: sheet={} blocks={} formulas={} choose={} long={} fixed={} "
                    "elapsed={:.2f}s soffice_rss_mb={}",
                    sheet_name,
                    len(blocks),
                    sheet_formulas,
                    sheet_choose,
                    sheet_scanned,
                    sheet_fixed,
                    time.monotonic() - sheet_started,
                    _proc_rss_mb(self.session.proc.pid if self.session.proc else None),
                )
        except Exception as e:
            _raise_if_rss_killed(watch, e)
            raise
        self.log.info(
            "fix_long_choose done: sheets={}, formulas={}, choose_seen={}, long_choose={}, fixed={}, "
            "unfixable={}, skipped_hidden={}, peak_soffice_mb={:.0f}, limit_mb={:.0f}, elapsed={:.1f}s",
            len(self.sheet_names),
            formulas_scanned,
            choose_seen,
            scanned,
            fixed,
            len(unfixable),
            skipped_hidden,
            watch.peak_mb,
            watch.limit_mb,
            time.monotonic() - started,
        )
        return {
            "fixed": fixed,
            "unfixable": unfixable,
            "scanned": scanned,
            "skipped_hidden": skipped_hidden,
            "formulas_scanned": formulas_scanned,
            "choose_seen": choose_seen,
        }
