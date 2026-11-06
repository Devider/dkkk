"""Unit tests for the soffice RSS limit parser and watch (lo_backend)."""

import time
from types import SimpleNamespace

import pytest

from aigw_service.api.v1 import lo_backend as lb


@pytest.mark.parametrize(
    ("raw", "expected_mb"),
    [
        ("100m", 100.0),
        ("100M", 100.0),
        ("3G", 3072.0),
        ("3g", 3072.0),
        ("1.5G", 1536.0),
        ("512k", 0.5),
        ("3GiB", 3072.0),
        ("3GB", 3072.0),
        ("250", 250.0),
        (" 200 m ", 200.0),
        ("1073741824b", 1024.0),
    ],
)
def test_parse_memory_limit_ok(raw, expected_mb):
    assert lb.parse_memory_limit(raw) == pytest.approx(expected_mb)


@pytest.mark.parametrize("raw", ["", "abc", "3x", "-3G", "12 t", "m", None])
def test_parse_memory_limit_invalid(raw):
    with pytest.raises(ValueError, match="unsupported memory value"):
        lb.parse_memory_limit(raw)


def test_env_default(monkeypatch):
    monkeypatch.delenv("LO_MEMORY_LIMIT", raising=False)
    assert lb.get_memory_limit_mb() == pytest.approx(3072.0)


def test_env_override(monkeypatch):
    monkeypatch.setenv("LO_MEMORY_LIMIT", "100m")
    assert lb.get_memory_limit_mb() == pytest.approx(100.0)


def test_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LO_MEMORY_LIMIT", "12xx")
    assert lb.get_memory_limit_mb() == pytest.approx(3072.0)


class _FakeProc:
    def __init__(self):
        self.pid = 424242
        self.killed = False

    def kill(self):
        self.killed = True


class _FakeSession(SimpleNamespace):
    def __init__(self):
        super().__init__(proc=_FakeProc())


def test_watch_kills_soffice_on_exceed(monkeypatch):
    readings = iter([10.0, 5000.0])
    monkeypatch.setattr(lb, "_proc_rss_mb", lambda pid: next(readings, 5000.0))
    session = _FakeSession()

    watch = lb.SofficeRssWatch(session, label="fix_long_choose", limit_mb=3072, interval=0.01)
    with watch:
        watch.current_sheet = "Выбрать язык"
        deadline = time.monotonic() + 5
        while not watch.triggered and time.monotonic() < deadline:
            time.sleep(0.01)

    assert session.proc.killed
    assert watch.exceeded_mb == pytest.approx(5000.0)
    message = str(watch.error())
    assert "fix_long_choose" in message
    assert "Выбрать язык" in message


def test_watch_stays_idle_below_limit(monkeypatch):
    readings = iter([10.0, 100.0, 200.0])
    monkeypatch.setattr(lb, "_proc_rss_mb", lambda pid: next(readings, 200.0))
    session = _FakeSession()

    watch = lb.SofficeRssWatch(session, label="save_as_xlsx", limit_mb=3072, interval=0.01)
    with watch:
        time.sleep(0.05)

    assert not session.proc.killed
    assert not watch.triggered
    assert watch.error() is None
    assert watch.peak_mb == pytest.approx(200.0)


def test_watch_explicit_start_stop_is_idempotent(monkeypatch):
    monkeypatch.setattr(lb, "_proc_rss_mb", lambda pid: 100.0)
    watch = lb.SofficeRssWatch(_FakeSession(), label="soffice-session", limit_mb=3072, interval=0.01)

    watch.stop()  # safe before start
    watch.start()
    assert watch._thread.is_alive()
    watch.stop()
    assert not watch._thread.is_alive()
    watch.stop()  # safe twice


def test_raise_if_rss_killed_translates_triggered_watch():
    watch = lb.SofficeRssWatch(_FakeSession(), label="soffice-session", limit_mb=100)
    watch.exceeded_mb = 5000.0

    with pytest.raises(RuntimeError, match="exceeded LO_MEMORY_LIMIT.*soffice-session"):
        lb._raise_if_rss_killed(watch, RuntimeError("Binary URP bridge disposed"))


def test_raise_if_rss_killed_ignores_none_watch():
    lb._raise_if_rss_killed(None, RuntimeError("Binary URP bridge disposed"))
