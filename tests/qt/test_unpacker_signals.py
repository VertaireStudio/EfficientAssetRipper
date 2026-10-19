"""Tests for `core.unpacker.UnpackerProcess` with a stubbed QProcess.

We don't launch a real CUE4ParseCLI here. Instead we replace the internal
QProcess instance with a stub that lets the test feed bytes into _on_stdout
and assert which signal got emitted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from PySide6.QtCore import QByteArray, QObject, QProcess, Signal

from core.unpacker import UnpackerProcess

pytestmark = pytest.mark.qt


# ---------------------------------------------------------------------------
# Stub QProcess (just enough for the readyReadStandardOutput pathway)
# ---------------------------------------------------------------------------

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
        # Escalation counters: a wedged CLI ignores SIGTERM, so terminate()
        # deliberately leaves the state Running — only kill() ends it.
        self.terminate_calls = 0
        self.kill_calls = 0

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

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
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

    # Test-only helpers
    def feed(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self._buf.extend(line.encode("utf-8"))
        self.readyReadStandardOutput.emit()

    def feed_chunk(self, raw: bytes) -> None:
        self._buf.extend(raw)
        self.readyReadStandardOutput.emit()


# ---------------------------------------------------------------------------
# Fixture: pre-built unpacker with a stub process attached
# ---------------------------------------------------------------------------

@pytest.fixture
def unpacker_with_stub(qtbot):
    up = UnpackerProcess()
    qtbot.add_widget = qtbot.addWidget  # alias for safety
    proc = StubQProcess(up)
    proc.readyReadStandardOutput.connect(up._on_stdout)
    proc.readyReadStandardError.connect(up._on_stderr)
    proc.errorOccurred.connect(up._on_error)  # matching UnpackerProcess.start
    up._proc = proc
    return up, proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_initialized_signal_fires_on_init_ndjson(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    msg = {
        "type": "init_done",
        "archive_count": 3,
        "unmounted_count": 1,
        "file_count": 100,
        "keys_submitted": 2,
        "loose_file_count": 5,
        "unmounted_archives": [{"name": "pakchunk0.pak", "guid": "00000000000000000000000000000000"}],
    }
    with qtbot.waitSignal(up.initialized, timeout=1000) as sig:
        proc.feed(json.dumps(msg))
    assert sig.args == [3, 1, 100, 2, 5, [{"name": "pakchunk0.pak", "guid": "00000000000000000000000000000000"}]]


def test_initialized_signal_defaults_unmounted_archives_to_empty_list(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    msg = {
        "type": "init_done",
        "archive_count": 1,
        "unmounted_count": 0,
        "file_count": 50,
        "keys_submitted": 0,
        "loose_file_count": 0,
        # Older CLI builds omit unmounted_archives entirely.
    }
    with qtbot.waitSignal(up.initialized, timeout=1000) as sig:
        proc.feed(json.dumps(msg))
    assert sig.args == [1, 0, 50, 0, 0, []]


def test_browse_result_signal_carries_entries(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {
        "type": "browse_result",
        "path": "/Game/Foo",
        "entries": [{"name": "Bar", "is_folder": True}],
    }
    with qtbot.waitSignal(up.browse_result, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[0] == "/Game/Foo"
    assert sig.args[1] == [{"name": "Bar", "is_folder": True}]


def test_exports_listed_carries_error_field(unpacker_with_stub, qtbot):
    """The CLI explains an empty export list; that reason must not be dropped."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "exports_listed",
        "path": "/Game/Foo.uasset",
        "export_count": 0,
        "exports": [],
        "error": "a .usmap mappings file is required",
    }
    with qtbot.waitSignal(up.exports_listed, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[0] == "/Game/Foo.uasset"
    assert sig.args[1] == []
    assert sig.args[2] == "a .usmap mappings file is required"


def test_exports_listed_error_defaults_to_empty_string(unpacker_with_stub, qtbot):
    """A successful listing carries no error, not None."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "exports_listed",
        "path": "/Game/Foo.uasset",
        "exports": [{"name": "Foo", "export_type": "Texture2D", "outer": ""}],
    }
    with qtbot.waitSignal(up.exports_listed, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[2] == ""


def test_export_done_emitted_with_succeeded_and_failed_lists(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {
        "type": "export_done",
        "succeeded": ["/Game/A.uasset", "/Game/B.uasset"],
        "failed": [{"path": "/Game/C.uasset", "reason": "missing key"}],
    }
    with qtbot.waitSignal(up.export_done, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[0] == ["/Game/A.uasset", "/Game/B.uasset"]
    assert sig.args[1][0]["reason"] == "missing key"


def test_progress_signal_emitted(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {"type": "progress", "current": 7, "total": 100, "message": "halfway"}
    with qtbot.waitSignal(up.progress, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args == [7, 100, "halfway"]


def test_partial_line_buffered_until_newline(unpacker_with_stub, qtbot):
    """A JSON line split across two reads should not be parsed prematurely."""
    up, proc = unpacker_with_stub
    payload = {"type": "warning", "message": "split-line warning"}
    raw = (json.dumps(payload) + "\n").encode("utf-8")
    half = len(raw) // 2

    # First half should not trigger a signal
    with qtbot.assertNotEmitted(up.warning, wait=200):
        proc.feed_chunk(raw[:half])

    # Second half completes the line — signal fires
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed_chunk(raw[half:])
    assert sig.args[0] == "split-line warning"


def test_invalid_ndjson_emits_no_signal_no_crash(unpacker_with_stub, qtbot):
    """Garbage stdout from CLI should not crash the parser."""
    up, proc = unpacker_with_stub
    with qtbot.assertNotEmitted(up.error, wait=200):
        proc.feed("this is not json at all")


def test_unknown_message_type_logged_not_signaled(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.assertNotEmitted(up.warning, wait=200):
        proc.feed(json.dumps({"type": "totally_unknown_type", "x": 1}))


def test_send_writes_json_with_newline(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.browse("/Game/X")
    assert proc.write_log
    payload = proc.write_log[0]
    assert payload.endswith(b"\n")
    parsed = json.loads(payload.decode("utf-8").strip())
    assert parsed == {"cmd": "browse", "path": "/Game/X"}


def test_send_when_not_running_emits_error(qtbot):
    """When _proc is None, sending should emit an error signal, not crash."""
    up = UnpackerProcess()
    with qtbot.waitSignal(up.error, timeout=1000) as sig:
        up.browse("/X")
    assert "not running" in sig.args[0].lower()


def test_warning_message_signal(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "warning", "message": "watch out"}))
    assert sig.args[0] == "watch out"


def test_error_message_signal(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.error, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "error", "message": "boom"}))
    assert sig.args[0] == "boom"


def test_oversized_ndjson_line_kills_child_and_emits_error(unpacker_with_stub, qtbot, monkeypatch):
    """A runaway, newline-less stream must abort rather than buffer indefinitely."""
    up, proc = unpacker_with_stub

    # Shrink the cap so the test doesn't have to allocate megabytes.
    import core.unpacker as up_mod
    monkeypatch.setattr(up_mod, "_MAX_NDJSON_LINE_BYTES", 64)

    # Hand it 256 bytes of garbage with no newline.
    payload = b"x" * 256
    with qtbot.waitSignal(up.error, timeout=1000) as sig:
        proc.feed_chunk(payload)
    assert "oversized" in sig.args[0].lower()
    # The stub's kill() flips state to NotRunning.
    assert proc.state() == QProcess.ProcessState.NotRunning


def test_unpacker_panel_clamps_negative_initialized_counts(qtbot, mock_qsettings):
    """init_done with negative values must not crash the GUI; counts are clamped."""
    from gui.unpacker_panel import UnpackerPanel

    panel = UnpackerPanel()
    qtbot.addWidget(panel)

    # Direct slot call avoids needing a real CLI subprocess.
    panel._on_initialized(-3, -1, -100, -2, -5)
    assert panel._mounted is True
    # Status text should reflect 0-clamped values, not negatives.
    assert "Mounted: 0 archives" in panel._mount_info.text()


def test_unpacker_panel_cancel_export_public_alias(qtbot, mock_qsettings):
    """`cancel_export()` must exist as a public alias on the panel."""
    from gui.unpacker_panel import UnpackerPanel

    panel = UnpackerPanel()
    qtbot.addWidget(panel)
    # The public method should exist and be safely callable when not exporting.
    assert callable(getattr(panel, "cancel_export", None))
    panel.cancel_export()


# ---------------------------------------------------------------------------
# CLI hardening contract — events the Python side must dispatch cleanly
# ---------------------------------------------------------------------------

def test_aes_keys_malformed_warning_dispatches(unpacker_with_stub, qtbot):
    """The CLI emits this warning when aes_keys is not a JArray (Phase 3.4)."""
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed(json.dumps({
            "type": "warning",
            "message": "aes_keys malformed; ignoring",
        }))
    assert "aes_keys" in sig.args[0]


def test_aes_key_invalid_warning_is_generic(unpacker_with_stub, qtbot):
    """AES error messages from the CLI are generic — must not echo raw bytes (Phase 2.3)."""
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed(json.dumps({
            "type": "warning",
            "message": "Key 00000000000000000000000000000000: invalid (length or format)",
        }))
    msg = sig.args[0]
    assert "invalid" in msg
    assert "length or format" in msg


def test_path_traversal_in_export_done_failed_list(unpacker_with_stub, qtbot):
    """Export of `../../foo.uasset` must surface as a failed entry, not a crash (Phase 1.1)."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "export_done",
        "succeeded": [],
        "failed": [
            {"path": "../../etc/passwd.uasset", "error": "Path escapes root: ../../etc"},
        ],
        "total": 1,
    }
    with qtbot.waitSignal(up.export_done, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[0] == []
    assert sig.args[1][0]["path"].startswith("../")
    assert "escapes" in sig.args[1][0]["error"].lower()


def test_export_done_cancellation_fields_flow_through(unpacker_with_stub, qtbot):
    """cancel/remaining/warning_count ride export_done so the panel can say
    "Export cancelled: N succeeded, M failed, K not attempted"."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "export_done",
        "succeeded": ["/Game/A.uasset"],
        "failed": [],
        "total": 10,
        "cancelled": True,
        "remaining": 9,
        "warning_count": 3,
    }
    with qtbot.waitSignal(up.export_done, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[0] == ["/Game/A.uasset"]
    assert sig.args[2] is True
    assert sig.args[3] == 9
    assert sig.args[4] == 3


def test_export_done_defaults_when_cancellation_fields_missing(unpacker_with_stub, qtbot):
    """Older CLI payloads (no fields) must still dispatch cleanly."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "export_done",
        "succeeded": ["/Game/A.uasset"],
        "failed": [],
        "total": 1,
    }
    with qtbot.waitSignal(up.export_done, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[2] is False
    assert sig.args[3] == 0
    assert sig.args[4] == 0


def test_input_line_too_large_error_dispatches(unpacker_with_stub, qtbot):
    """The CLI's stdin line-cap surfaces as an error event (Phase 3.2)."""
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.error, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "error", "message": "input line too large"}))
    assert "too large" in sig.args[0]


def test_serialize_thread_cap_warning_dispatches(unpacker_with_stub, qtbot):
    """Bounded serialize-thread overflow surfaces as a warning (Phase 3.3)."""
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed(json.dumps({
            "type": "warning",
            "message": "Serialize thread cap reached; skipping",
        }))
    assert "thread cap" in sig.args[0]


def test_oodle_hash_mismatch_warning_dispatches(unpacker_with_stub, qtbot):
    """Hash-pinned downloads must report mismatch as a warning (Phase 1.4)."""
    up, proc = unpacker_with_stub
    msg = (
        "Oodle download hash mismatch (expected aabb, got ccdd). Refusing to extract."
    )
    with qtbot.waitSignal(up.warning, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "warning", "message": msg}))
    assert "hash mismatch" in sig.args[0].lower()


def test_utf8_round_trip_through_dispatch(unpacker_with_stub, qtbot):
    """CJK / non-ASCII filenames must round-trip cleanly (Phase 2.1)."""
    up, proc = unpacker_with_stub
    payload = {
        "type": "browse_result",
        "path": "/Game/サウンド",
        "entries": [{"name": "音乐.uasset", "is_folder": False, "asset_type": "Audio"}],
    }
    with qtbot.waitSignal(up.browse_result, timeout=1000) as sig:
        # Force the chunk to be the exact UTF-8 bytes the CLI would emit.
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        proc.feed_chunk(raw)
    assert sig.args[0] == "/Game/サウンド"
    assert sig.args[1][0]["name"] == "音乐.uasset"


# ---------------------------------------------------------------------------
# scan_types — bulk type-cache walk
# ---------------------------------------------------------------------------

def test_scan_types_command_writes_correct_ndjson(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.scan_types()
    assert proc.write_log
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload == {"cmd": "scan_types"}


def test_types_scan_progress_signal(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.types_scan_progress, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "types_scan_progress", "current": 250, "total": 1000}))
    assert sig.args == [250, 1000]


def test_types_scan_batch_intermediate_does_not_emit_done(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {
        "type": "types_scan_batch",
        "entries": [{"path": "/G/A.uasset", "exports": [{"name": "x", "export_type": "Texture2D"}]}],
        "final": False,
    }
    with qtbot.waitSignal(up.types_scan_batch, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    assert sig.args[1] is False
    assert sig.args[0][0]["path"] == "/G/A.uasset"


def test_types_scan_batch_final_emits_done_with_counts(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {
        "type": "types_scan_batch",
        "entries": [],
        "final": True,
        "error_count": 3,
        "total_packages": 4200,
    }
    with qtbot.waitSignal(up.types_scan_done, timeout=1000) as done_sig:
        proc.feed(json.dumps(payload))
    assert done_sig.args == [3, 4200]


# ---------------------------------------------------------------------------
# FMOD bank scan / export
# ---------------------------------------------------------------------------

def test_scan_fmod_banks_command_writes_correct_ndjson(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.scan_fmod_banks()
    assert proc.write_log
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload == {"cmd": "scan_fmod_banks"}


def test_export_fmod_audio_command_writes_correct_ndjson(unpacker_with_stub):
    up, proc = unpacker_with_stub
    entries = [{
        "bank_vfs_path": "Game/Content/FMOD/Desktop/SFX.bank",
        "stream_index": 3,
        "target_name": "0003_footstep",
        "target_folder": "Game/Content/FMOD/Desktop/SFX",
    }]
    up.export_fmod_audio(entries, "C:/out")
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload == {
        "cmd": "export_fmod_audio",
        "output_dir": "C:/out",
        "entries": entries,
    }


def test_fmod_scan_result_signal(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    payload = {
        "type": "fmod_scan_result",
        "found": True,
        "total_banks": 1,
        "total_streams": 2,
        "banks": [{
            "bank_vfs_path": "Game/Content/FMOD/Desktop/SFX.bank",
            "bank_name": "SFX.bank",
            "streams": [
                {"index": 1, "name": "jump", "sample_rate": 48000, "duration_ms": 500},
                {"index": 2, "name": "", "sample_rate": 44100, "duration_ms": 1200},
            ],
        }],
    }
    with qtbot.waitSignal(up.fmod_scan_result, timeout=1000) as sig:
        proc.feed(json.dumps(payload))
    result = sig.args[0]
    assert result["found"] is True
    assert result["banks"][0]["streams"][0]["name"] == "jump"


# ---------------------------------------------------------------------------
# Multithreaded export — "threads" field rides both export commands
# ---------------------------------------------------------------------------

def test_export_command_includes_threads_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out", threads=4)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export"
    assert payload["threads"] == 4


def test_export_command_omits_threads_when_auto(unpacker_with_stub):
    """None means CLI-side auto sizing — the field must not be sent as 0"""
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out", threads=None)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "threads" not in payload


def test_export_folder_command_includes_threads_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("/Game/Foo", "C:/out", threads=8)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export_folder"
    assert payload["threads"] == 8


def test_export_folder_command_omits_threads_when_auto(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("/Game/Foo", "C:/out", threads=None)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "threads" not in payload


def test_wwise_audio_command_includes_threads_when_set(unpacker_with_stub):
    """Parallel WWise export reuses the export worker count."""
    up, proc = unpacker_with_stub
    up.export_wwise_audio([{"wem_vfs_path": "/G/A.wem", "target_name": "A"}],
                          "C:/out", threads=4)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export_wwise_audio"
    assert payload["threads"] == 4


def test_wwise_audio_command_omits_threads_when_auto(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_wwise_audio([{"wem_vfs_path": "/G/A.wem", "target_name": "A"}],
                          "C:/out", threads=None)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "threads" not in payload


def test_wwise_audio_command_ignores_non_positive_threads(unpacker_with_stub):
    """0/negative must not ride along — CLI treats 0 as auto anyway, and the
    parent sends None for auto, so a stray 0 is a caller bug we filter."""
    up, proc = unpacker_with_stub
    up.export_wwise_audio([{"wem_vfs_path": "/G/A.wem", "target_name": "A"}],
                          "C:/out", threads=0)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "threads" not in payload


def test_fmod_audio_command_includes_threads_when_set(unpacker_with_stub):
    """Parallel FMOD subsong export reuses the export worker count."""
    up, proc = unpacker_with_stub
    up.export_fmod_audio([{"bank_vfs_path": "Game/B.bank", "stream_index": 1,
                           "target_name": "jump"}], "C:/out", threads=4)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export_fmod_audio"
    assert payload["threads"] == 4


def test_fmod_audio_command_omits_threads_when_auto(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_fmod_audio([{"bank_vfs_path": "Game/B.bank", "stream_index": 1,
                           "target_name": "jump"}], "C:/out", threads=None)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "threads" not in payload


# ---------------------------------------------------------------------------
# Export formats — mesh_format / webp options ride the export commands
# ---------------------------------------------------------------------------

def test_export_command_carries_mesh_and_webp_options(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out", mesh_format="gltf",
              webp_mode="lossy", webp_quality=85)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["mesh_format"] == "gltf"
    assert payload["webp_mode"] == "lossy"
    assert payload["webp_quality"] == 85


def test_export_folder_command_carries_mesh_and_webp_options(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("/Game/Foo", "C:/out", mesh_format="psk",
                     webp_mode="lossless", webp_quality=100)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["mesh_format"] == "psk"
    assert payload["webp_mode"] == "lossless"
    assert payload["webp_quality"] == 100


def test_wwise_command_carries_audio_format_choice(unpacker_with_stub):
    """mp3/webm targets ride export_wwise_audio like wav/ogg always did."""
    up, proc = unpacker_with_stub
    up.export_wwise_audio([{"wem_vfs_path": "/G/A.wem", "target_name": "A"}],
                          "C:/out", audio_format="webm")
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["audio_format"] == "webm"


# ---------------------------------------------------------------------------
# Memory policy — rides the export commands only when active
# ---------------------------------------------------------------------------

def test_export_command_carries_memory_policy_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out",
              memory_limit_percent=60, physical_only=True)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["memory_limit_percent"] == 60
    assert payload["physical_only"] is True


def test_export_command_omits_memory_policy_when_unset(unpacker_with_stub):
    """Defaults (0 / False) follow the threads/gpu convention — no fields."""
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out")
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "memory_limit_percent" not in payload
    assert "physical_only" not in payload


def test_export_command_omits_zero_percent_without_physical(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export(["/Game/A.uasset"], "C:/out", memory_limit_percent=0,
              physical_only=False)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "memory_limit_percent" not in payload


def test_export_folder_command_carries_memory_policy_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("/Game/Foo", "C:/out",
                     memory_limit_percent=40, physical_only=True)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export_folder"
    assert payload["memory_limit_percent"] == 40
    assert payload["physical_only"] is True


def test_export_folder_command_omits_memory_policy_when_unset(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_folder("/Game/Foo", "C:/out")
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "memory_limit_percent" not in payload
    assert "physical_only" not in payload


def test_wwise_audio_command_carries_memory_policy_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_wwise_audio([{"wem_vfs_path": "/G/A.wem", "target_name": "A"}],
                          "C:/out", memory_limit_percent=50, physical_only=False)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["memory_limit_percent"] == 50
    assert "physical_only" not in payload


def test_fmod_audio_command_carries_memory_policy_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_fmod_audio([{"bank_vfs_path": "Game/B.bank", "stream_index": 1,
                           "target_name": "jump"}], "C:/out",
                         memory_limit_percent=30, physical_only=True)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["memory_limit_percent"] == 30
    assert payload["physical_only"] is True


def test_video_command_carries_memory_policy_when_set(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_video([{"vfs_path": "/G/V.bk2", "kind": "raw_video"}], "C:/out",
                    memory_limit_percent=80, physical_only=True)
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert payload["cmd"] == "export_video"
    assert payload["memory_limit_percent"] == 80
    assert payload["physical_only"] is True


def test_video_command_omits_memory_policy_when_unset(unpacker_with_stub):
    up, proc = unpacker_with_stub
    up.export_video([{"vfs_path": "/G/V.bk2", "kind": "raw_video"}], "C:/out")
    payload = json.loads(proc.write_log[0].decode("utf-8").strip())
    assert "memory_limit_percent" not in payload
    assert "physical_only" not in payload


# ---------------------------------------------------------------------------
# CLI "info" messages (e.g. the mount-time memory policy) dispatch cleanly
# ---------------------------------------------------------------------------

def test_info_message_dispatches_to_info_signal(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.info, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "info", "message": "Memory limit active: 4.0 GB"}))
    assert sig.args == ["Memory limit active: 4.0 GB"]


def test_info_message_without_field_emits_empty_string(unpacker_with_stub, qtbot):
    up, proc = unpacker_with_stub
    with qtbot.waitSignal(up.info, timeout=1000) as sig:
        proc.feed(json.dumps({"type": "info"}))
    assert sig.args == [""]


# ---------------------------------------------------------------------------
# start() — env overrides (DOTNET_gcServer) + started_gc_mode bookkeeping
# ---------------------------------------------------------------------------

class FakeLaunchProcess(QObject):
    """Minimal QProcess stand-in for exercising UnpackerProcess.start()."""
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)

    # Enum namespaces UnpackerProcess.start() reads off the class itself.
    ProcessState = QProcess.ProcessState
    ProcessChannelMode = QProcess.ProcessChannelMode
    ProcessError = QProcess.ProcessError

    def __init__(self, parent=None):
        super().__init__(parent)
        self.env = None
        self.started = False

    def setProgram(self, *_): pass

    def setProcessChannelMode(self, *_): pass

    def setProcessEnvironment(self, env):
        self.env = env

    def start(self):
        self.started = True

    def waitForStarted(self, _timeout=0):
        return True

    def state(self):
        return QProcess.ProcessState.NotRunning


def _launch_with(monkeypatch, qtbot, tmp_path, env_overrides):
    import core.unpacker as unpacker_mod

    exe = tmp_path / "CUE4ParseCLI"
    exe.write_bytes(b"")
    fake = FakeLaunchProcess()
    factory = lambda parent=None, **kw: fake
    # start() also reads enum namespaces off the class itself.
    factory.ProcessState = QProcess.ProcessState
    factory.ProcessChannelMode = QProcess.ProcessChannelMode
    factory.ProcessError = QProcess.ProcessError
    monkeypatch.setattr(unpacker_mod, "QProcess", factory)
    up = UnpackerProcess()
    ok = up.start(str(exe), env_overrides=env_overrides)
    return up, fake, ok


def test_start_with_workstation_override(qtbot, monkeypatch, tmp_path):
    up, fake, ok = _launch_with(monkeypatch, qtbot, tmp_path,
                                {"DOTNET_gcServer": "0"})
    assert ok is True
    assert fake.started is True
    assert up.started_gc_mode == "workstation"
    assert fake.env is not None
    assert fake.env.value("DOTNET_gcServer") == "0"


def test_start_default_gc_mode_is_server(qtbot, monkeypatch, tmp_path):
    up, _fake, ok = _launch_with(monkeypatch, qtbot, tmp_path, None)
    assert ok is True
    assert up.started_gc_mode == "server"


def test_missing_cli_reports_error(qtbot, monkeypatch, tmp_path):
    """A nonexistent exe path must emit `error` and return False."""
    up = UnpackerProcess()
    errors = []
    up.error.connect(errors.append)
    assert up.start(str(tmp_path / "nope")) is False
    assert errors and "not found" in errors[0]


# ---------------------------------------------------------------------------
# Cancel: immediate shutdown (no wait-for-response)
# ---------------------------------------------------------------------------

def _sent_cmds(proc):
    """Decode every line written to stdin into command names."""
    return [json.loads(bytes(line).decode("utf-8"))["cmd"]
            for line in proc.write_log]


def test_cancel_during_export_terminates_immediately(unpacker_with_stub):
    """An in-flight export is torn down on the spot: terminate() fires in
    the cancel call itself, with no graceful token sent and no wait for
    the CLI to acknowledge."""
    up, proc = unpacker_with_stub
    up._cancel_kill_delay_ms = 50  # keep the kill bound fast in tests

    up._send({"cmd": "export"})
    assert up._outstanding_exports == 1
    up.cancel()

    # Immediate, not "on the next timer tick".
    assert proc.terminate_calls == 1
    assert up._outstanding_exports == 0
    assert up.cancel_shutdown_requested is True
    # No graceful cancel token at all — the CLI is being shut down, not
    # negotiated with.
    assert "cancel" not in _sent_cmds(proc)


def test_wedged_cli_killed_after_terminate_bound(unpacker_with_stub, qtbot):
    """A CLI that ignores SIGTERM (wedged export thread) is kill()ed after
    the short bound; the bound is also disarmed if the process dies on its
    own before it fires."""
    up, proc = unpacker_with_stub
    up._cancel_kill_delay_ms = 50
    up._send({"cmd": "export"})
    up.cancel()

    # Stub terminate() deliberately leaves the state Running (wedged CLI).
    qtbot.waitUntil(lambda: proc.kill_calls == 1, timeout=3000)
    assert proc.state() == QProcess.ProcessState.NotRunning


def test_graceful_cli_exit_disarms_cancel_kill_timer(unpacker_with_stub, qtbot):
    """If the CLI exits after the terminate (before the kill bound fires),
    the kill timer is disarmed and no hard kill happens."""
    up, proc = unpacker_with_stub
    up._cancel_kill_delay_ms = 60_000  # bound never fires in this test
    up._send({"cmd": "export"})
    up.cancel()
    assert up._cancel_timer is not None

    # Simulate the CLI exiting cleanly after the SIGTERM.
    proc._state = QProcess.ProcessState.NotRunning
    up._on_finished(0, QProcess.ExitStatus.NormalExit)

    qtbot.waitUntil(lambda: up._cancel_timer is None, timeout=2000)
    assert proc.kill_calls == 0


def test_idle_cancel_sends_graceful_token(unpacker_with_stub):
    """Cancelling with no export leg in flight must not tear the session
    down — it sends the graceful token the CLI honors at its boundary."""
    up, proc = unpacker_with_stub
    up.cancel()
    assert up._outstanding_exports == 0
    assert up._cancel_timer is None
    assert _sent_cmds(proc) == ["cancel"]
    assert proc.terminate_calls == 0


def test_idle_cancelled_ack_clears_stale_shipping_count(unpacker_with_stub):
    """An ack without an exporting field (idle cancel) zeroes the count so
    no later cancel decision ever runs against a stale leg count."""
    up, proc = unpacker_with_stub
    up._send({"cmd": "export"})
    up._on_export_report()          # leg finished before the cancel lands
    assert up._outstanding_exports == 0

    up.cancel()
    proc.feed(json.dumps({"type": "cancelled"}))
    assert up._outstanding_exports == 0
    assert up._cancel_timer is None


def test_process_error_after_cancel_shutdown_is_suppressed(unpacker_with_stub):
    """The cancel teardown's own kill reports ProcessError.Crashed — that is
    expected and must not surface as a 'CUE4ParseCLI process error'."""
    up, proc = unpacker_with_stub
    up._cancel_kill_delay_ms = 50
    errors = []
    up.error.connect(errors.append)

    up._send({"cmd": "export"})
    up.cancel()
    assert up.cancel_shutdown_requested is True
    proc.errorOccurred.emit(QProcess.ProcessError.Crashed)
    assert errors == []


def test_process_error_without_cancel_still_reported(unpacker_with_stub):
    """A genuine crash (no user cancel in progress) keeps erroring."""
    up = unpacker_with_stub[0]
    errors = []
    up.error.connect(errors.append)
    up._on_error(QProcess.ProcessError.Crashed)
    assert errors and "process error" in errors[0]
