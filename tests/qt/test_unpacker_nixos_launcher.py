"""NixOS launcher resolution tests for UnpackerProcess.start (...).

Covers the three outcomes decided before the QProcess is launched:
- nix-ld available → plain launch (program = the CLI path, no prefix).
- no nix-ld + steam-run found → program = steam-run, args = [CLI path].
- neither → start aborted with an actionable error signal.
Off-NixOS behavior is unchanged. QProcess is stubbed the same way as
test_unpacker_gpu_signals — nothing is ever actually spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QObject, QProcess, Signal

from core import platform_detect
from core.unpacker import UnpackerProcess

pytestmark = pytest.mark.qt


class StubQProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = QProcess.ProcessState.NotRunning
        self.program = ""
        self.args: list[str] = []
        self.started = False
        self.start_attempts = 0

    def state(self):
        return self._state

    def setProgram(self, program):
        self.program = program

    def setArguments(self, args):
        self.args = list(args)

    def setProcessChannelMode(self, *args):
        pass

    def setProcessEnvironment(self, *args):
        pass

    def start(self):
        self.started = True
        self.start_attempts += 1

    def waitForStarted(self, _ms):
        return True

    def waitForFinished(self, _ms):
        return True

    def kill(self):
        pass

    def write(self, data: bytes) -> int:
        return len(data)

    def readAllStandardOutput(self) -> QByteArray:
        return QByteArray()

    def readAllStandardError(self) -> QByteArray:
        return QByteArray()


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    exe = tmp_path / "CUE4ParseCLI"
    exe.write_bytes(b"\x7fELF")
    return exe


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    platform_detect.reset_cache()
    yield
    platform_detect.reset_cache()


def _make(monkeypatch, tmp_path: Path, fake_cli: Path) -> tuple[UnpackerProcess, StubQProcess, list]:
    """Patch unpacker.QProcess with a shim wrapping the stub, then start()."""
    errors: list[str] = []
    stub_holder: list[StubQProcess] = []

    class _QProcShim:
        """Namespace+constructor+proxy: unpacker.py imports QProcess as one
        name and uses its enums, constructs it and calls QProcess methods."""
        ProcessChannelMode = QProcess.ProcessChannelMode
        ProcessState = QProcess.ProcessState
        ProcessError = QProcess.ProcessError
        ExitStatus = QProcess.ExitStatus

        def __init__(self, parent=None):
            self._stub = StubQProcess(parent)
            stub_holder.append(self._stub)

        def __getattr__(self, name):
            # Only called for attrs not on the shim itself (setProgram, ...).
            return getattr(self._stub, name)

    monkeypatch.setattr("core.unpacker.QProcess", _QProcShim)
    u = UnpackerProcess()
    u.error.connect(errors.append)
    return u, u.start(str(fake_cli)), stub_holder, errors


def test_start_nixos_nix_ld_plain_launch(monkeypatch, tmp_path, fake_cli):
    monkeypatch.setattr(platform_detect, "is_nixos", lambda: True)
    monkeypatch.setattr(platform_detect, "has_nix_ld", lambda: True)
    u, started, stub_holder, errors = _make(monkeypatch, tmp_path, fake_cli)
    assert started
    stub = stub_holder[0]
    assert stub.program == str(fake_cli)
    assert stub.args == []
    assert errors == []


def test_start_nixos_steam_run_fallback(monkeypatch, tmp_path, fake_cli):
    monkeypatch.setattr(platform_detect, "is_nixos", lambda: True)
    monkeypatch.setattr(platform_detect, "has_nix_ld", lambda: False)
    monkeypatch.setattr(platform_detect, "steam_run_path", lambda: "/usr/bin/steam-run")
    u, started, stub_holder, errors = _make(monkeypatch, tmp_path, fake_cli)
    assert started
    stub = stub_holder[0]
    assert stub.program == "/usr/bin/steam-run"
    assert stub.args == [str(fake_cli)]
    assert errors == []
    # Sending of NDJSON commands must remain unchanged under the prefix.
    assert stub.started


def test_start_nixos_stuck_aborts_with_hint(monkeypatch, tmp_path, fake_cli):
    monkeypatch.setattr(platform_detect, "is_nixos", lambda: True)
    monkeypatch.setattr(platform_detect, "has_nix_ld", lambda: False)
    monkeypatch.setattr(platform_detect, "steam_run_path", lambda: "")
    u, started, stub_holder, errors = _make(monkeypatch, tmp_path, fake_cli)
    # No QProcess was ever constructed: start() bailed before touching it.
    assert not started
    assert stub_holder == []
    assert len(errors) == 1
    assert "nix-ld" in errors[0]
    assert "programs.nix-ld" in errors[0]


def test_start_non_nixos_plain_launch(monkeypatch, tmp_path, fake_cli):
    monkeypatch.setattr(platform_detect, "is_nixos", lambda: False)
    u, started, stub_holder, errors = _make(monkeypatch, tmp_path, fake_cli)
    assert started
    stub = stub_holder[0]
    assert stub.program == str(fake_cli)
    assert stub.args == []
    assert errors == []


def test_start_nixos_platform_probe_failure_still_launches(
    monkeypatch, tmp_path, fake_cli
):
    """A busted platform module must not block launches off the happy path."""
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(platform_detect, "is_nixos", boom)
    u, started, stub_holder, errors = _make(monkeypatch, tmp_path, fake_cli)
    assert started
    assert stub_holder[0].program == str(fake_cli)
    assert errors == []
