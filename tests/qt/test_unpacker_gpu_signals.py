"""GPU texture-decode plumbing tests.

Covers:
- `UnpackerProcess.initialize/export/export_folder` forwarding the resolved
  gpu preference into the outgoing NDJSON commands.
- `_dispatch` of the CLI's `gpu_status` NDJSON record onto the
  `gpu_status` signal.
- UnpackerPanel `_on_gpu_status` building the expected log line, and
  `_gpu_flag` resolving the Off setting explicitly to False.

GPU decode only touches the IPC surface, so the Qt-level tests mirror
existing StubQProcess-based patterns from test_unpacker_signals.py.
"""

from __future__ import annotations
import json

import pytest
from PySide6.QtCore import QByteArray, QObject, QProcess, Signal

import config
from core.unpacker import UnpackerProcess

pytestmark = pytest.mark.qt


class StubQProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buf = bytearray()
        self._err = bytearray()
        self._state = QProcess.ProcessState.Running
        self.write_log: list[bytes] = []

    def state(self):
        return self._state

    def setProgram(self, *_):
        pass

    def setProcessChannelMode(self, *_):
        pass

    def waitForStarted(self, _ms):
        return True

    def waitForFinished(self, _ms):
        return True

    def kill(self):
        self._state = QProcess.ProcessState.NotRunning

    def write(self, data: bytes) -> int:
        self.write_log.append(bytes(data))
        return len(data)

    def readAllStandardOutput(self) -> QByteArray:
        ba = QByteArray(bytes(self._buf))
        self._buf.clear()
        return ba

    def readAllStandardError(self) -> QByteArray:
        ba = QByteArray(bytes(self._err))
        self._err.clear()
        return ba

    def feed(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self._buf.extend(line.encode("utf-8"))
        self.readyReadStandardOutput.emit()


def _last_sent(proc) -> dict:
    assert proc.write_log, "no command was written to the stub process"
    return json.loads(bytes(proc.write_log[-1]).decode("utf-8"))


@pytest.fixture
def unpacker_with_stub(qtbot):
    up = UnpackerProcess()
    proc = StubQProcess(up)
    proc.readyReadStandardOutput.connect(up._on_stdout)
    proc.readyReadStandardError.connect(up._on_stderr)
    up._proc = proc
    return up, proc


# ---------------------------------------------------------------------------
# Outgoing command payload tests
# ---------------------------------------------------------------------------

def test_initialize_defaults_to_no_gpu_field(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.initialize("G:/Game", [], "GAME_UE5_4")
    cmd = _last_sent(proc)
    assert "gpu" not in cmd


def test_initialize_gpu_off_sends_false(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.initialize("G:/Game", [], "GAME_UE5_4", gpu=False)
    assert _last_sent(proc)["gpu"] is False


def test_initialize_gpu_on_sends_true(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.initialize("G:/Game", [], "GAME_UE5_4", gpu=True)
    assert _last_sent(proc)["gpu"] is True


def test_export_gpu_off_sends_false(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["path.upk"], "/tmp/out", gpu=False)
    assert _last_sent(proc)["gpu"] is False


def test_export_folder_gpu_off_sends_false(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("Game/Content", "/tmp/out", gpu=False)
    assert _last_sent(proc)["gpu"] is False


def test_export_defaults_to_no_gpu_field(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["path.upk"], "/tmp/out")
    assert "gpu" not in _last_sent(proc)


# ---------------------------------------------------------------------------
# gpu_status NDJSON dispatch tests
# ---------------------------------------------------------------------------

def test_gpu_status_dispatch_available(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.gpu_status, timeout=1000) as sig:
        proc.feed(json.dumps({
            "type": "gpu_status",
            "available": True,
            "device": "AMD Radeon RX 7600",
            "reason": "",
        }))
    assert sig.args == ["AMD Radeon RX 7600", True, ""]


def test_gpu_status_dispatch_unavailable_with_reason(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.gpu_status, timeout=1000) as sig:
        proc.feed(json.dumps({
            "type": "gpu_status",
            "available": False,
            "device": "",
            "reason": "no Vulkan GPU devices (CPU adapters excluded: slower than CPU decode)",
        }))
    assert sig.args == [
        "",
        False,
        "no Vulkan GPU devices (CPU adapters excluded: slower than CPU decode)",
    ]


def test_gpu_status_dispatch_defaults_missing_lines(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.gpu_status, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "gpu_status"}))
    assert sig.args == ["", False, ""]


# ---------------------------------------------------------------------------
# Panel-side log / setting resolution tests
# ---------------------------------------------------------------------------

@pytest.fixture
def panel(qtbot):
    from gui.unpacker_panel import UnpackerPanel
    p = UnpackerPanel()
    qtbot.addWidget(p)
    return p


def test_gpu_flag_off_resolves_false(panel, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "off")
    assert panel._gpu_flag() is False


def test_gpu_flag_on_and_auto_pass_through(panel, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "on")
    assert panel._gpu_flag() is None

    monkeypatch.setattr(config, "get", lambda key, *a, **k: "auto")
    assert panel._gpu_flag() is None


def test_gpu_status_available_logs_enabled_line(panel, qtbot, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "on")
    messages = []
    panel.log_message.connect(lambda m, lvl: messages.append((m, lvl)))

    panel._on_gpu_status("AMD Radeon RX 7600", True, "")

    assert messages == [("GPU texture decode: enabled — AMD Radeon RX 7600", "success")]


def test_gpu_status_available_auto_suffix(panel, qtbot, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "auto")
    messages = []
    panel.log_message.connect(lambda m, lvl: messages.append((m, lvl)))

    panel._on_gpu_status("AMD Radeon RX 7600", True, "")

    assert messages == [("GPU texture decode: enabled (auto) — AMD Radeon RX 7600", "success")]


def test_gpu_status_unavailable_logs_off_line(panel, qtbot, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "auto")
    messages = []
    panel.log_message.connect(lambda m, lvl: messages.append((m, lvl)))

    panel._on_gpu_status("", False, "no Vulkan GPU devices")

    assert messages == [("GPU texture decode: off — no Vulkan GPU devices", "info")]


def test_gpu_status_unavailable_blank_reason(panel, qtbot, monkeypatch):
    monkeypatch.setattr(config, "get", lambda key, *a, **k: "auto")
    messages = []
    panel.log_message.connect(lambda m, lvl: messages.append((m, lvl)))

    panel._on_gpu_status("", False, "")

    assert messages == [("GPU texture decode: off — no suitable GPU device", "info")]
