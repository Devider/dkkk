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
"""

import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

import loguru

logger = loguru.logger

LO_PROGRAM_CANDIDATES = (
    "/usr/lib64/libreoffice/program",
    "/usr/lib/libreoffice/program",
    "/usr/lib/python3/dist-packages",
)

UNO = None
_ENUMS: dict = {}


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
    if hasattr(uno, "getComponentContext") and hasattr(uno, "systemPathToFileUrl"):
        return uno
    return None


def ensure_uno(lo_program_dir: Optional[str] = None):
    """Import and expose the uno module, extending sys.path if needed.

    ``URE_BOOTSTRAP`` must point at LibreOffice's ``fundamentalrc`` even when
    ``uno`` is already importable (e.g. via system dist-packages) — without it
    the URP bridge dies with "Binary URP bridge disposed during call".
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
        if d not in sys.path:
            sys.path.insert(0, d)
        UNO = _try_import_uno()
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

    def __init__(self, port: Optional[int] = None, lo_program_dir: Optional[str] = None):
        ensure_uno(lo_program_dir)
        self.lo_program_dir = lo_program_dir
        self.port = port or self._free_port()
        self.ctx = None
        self.desktop = None
        self.proc: Optional[subprocess.Popen] = None
        self.profile_dir = None
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
        try:
            self._connect(self.START_TIMEOUT)
        except Exception:
            self.stop()
            raise

    def open_document(self, path: str):
        url = UNO.systemPathToFileUrl(os.path.abspath(path))
        doc = self.desktop.loadComponentFromURL(url, "_blank", 0, (_prop("Hidden", True), _prop("ReadOnly", True)))
        if doc is None:
            raise RuntimeError(f"LibreOffice failed to open {path}")
        return doc

    def stop(self):
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

    def __init__(self, session: LibreOfficeSession, path: str):
        self.session = session
        self.path = path
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

    def calculate_all(self):
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
