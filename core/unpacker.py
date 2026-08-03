"""Manages the CUE4ParseCLI subprocess and exposes Qt signals for the GUI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, Signal, Slot

log = logging.getLogger(__name__)

# A single NDJSON line should never exceed this. The CLI emits per-asset
# JSON objects which top out at low-double-digit KB; anything bigger is
# either a runaway export listing or a corrupt stream.
_MAX_NDJSON_LINE_BYTES = 16 * 1024 * 1024  # 16 MB

# VFS leaf extensions we treat as raw playable video — exported byte-for-byte
# without going through a UObject. The .bk2 case isn't decodable by Qt but is
# still extracted so the user can drag it into a dedicated player.
RAW_VIDEO_EXTENSIONS: tuple[str, ...] = (".bk2", ".mp4", ".webm", ".mov")


class UnpackerProcess(QObject):
    """Persistent subprocess wrapper around CUE4ParseCLI.exe.

    Communication is via NDJSON (one JSON object per line) over stdin/stdout.
    """

    # Signals
    initialized = Signal(int, int, int, int, int, list)  # archive_count, unmounted_count, file_count, keys_submitted, loose_file_count, unmounted_archives
    browse_result = Signal(str, list)         # path, list[dict{name, is_folder}]
    progress = Signal(int, int, str)          # current, total, message
    export_done = Signal(list, list)          # succeeded: list[str], failed: list[dict]
    props_result = Signal(str, list)          # path, list[dict{name, export_type, properties}]
    exports_listed = Signal(str, list, str)   # path, list[dict{name, export_type, outer}], error ("" if none)
    wwise_scan_result = Signal(dict)          # full scan result dict
    types_scan_progress = Signal(int, int)    # current, total
    types_scan_batch = Signal(list, bool)     # entries, is_final
    types_scan_done = Signal(int, int)        # error_count, total_packages
    warning = Signal(str)                     # warning message
    version_warning = Signal(str, str)        # (message, current_ue_version) — likely UE-version mismatch
    version_detected = Signal(str, str, str)  # (suggested_version, source_exe, file_version) or (None, None, reason)
    error = Signal(str)                       # error message
    process_ready = Signal()                  # CLI process started OK
    process_ended = Signal()                  # CLI process died / quit

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._buffer = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, cli_path: str) -> bool:
        """Launch the CUE4ParseCLI subprocess."""
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            log.warning("CUE4ParseCLI already running")
            return True

        exe = Path(cli_path)
        if not exe.is_file():
            self.error.emit(f"CUE4ParseCLI not found: {cli_path}")
            return False

        self._proc = QProcess(self)
        self._proc.setProgram(str(exe))
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.start()

        if not self._proc.waitForStarted(5000):
            msg = "Failed to start CUE4ParseCLI process"
            self.error.emit(msg)
            try:
                from core import crash_reporter
                if crash_reporter.is_installed():
                    crash_reporter.report_subprocess_crash(
                        "CUE4ParseCLI", f"{msg} (path: {cli_path})",
                        show_dialog=False,
                    )
            except Exception:
                log.exception("crash_reporter.report_subprocess_crash failed")
            return False

        self._buffer = ""
        log.info("CUE4ParseCLI started: %s", cli_path)
        self.process_ready.emit()
        return True

    def stop(self):
        """Gracefully shut down the subprocess."""
        if self._proc and self._proc.state() == QProcess.ProcessState.Running:
            self._send({"cmd": "quit"})
            if not self._proc.waitForFinished(3000):
                self._proc.kill()
                self._proc.waitForFinished(2000)
        self._proc = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() == QProcess.ProcessState.Running

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def initialize(self, game_dir: str, aes_keys: list[dict], ue_version: str = "GAME_UE5_4",
                   mappings_path: str = "") -> None:
        """Send init command to mount game archives."""
        self._send({
            "cmd": "init",
            "game_dir": game_dir,
            "aes_keys": aes_keys,
            "ue_version": ue_version,
            "mappings_path": mappings_path,
        })

    def browse(self, vfs_path: str = "/") -> None:
        """Request directory listing at *vfs_path*."""
        self._send({"cmd": "browse", "path": vfs_path})

    def detect_ue_version(self, game_dir: str) -> None:
        """Auto-detect UE version from game .exe FileVersionInfo."""
        self._send({"cmd": "detect_ue_version", "game_dir": game_dir})

    def export(self, asset_paths: list[str], output_dir: str,
               formats: Optional[dict[str, bool]] = None,
               texture_format: str = "png", audio_format: str = "wav") -> None:
        """Export specific assets."""
        self._send({
            "cmd": "export",
            "paths": asset_paths,
            "output_dir": output_dir,
            "formats": formats or {"mesh": True, "texture": True, "props": True,
                                   "animation": True, "audio": True, "video": True},
            "texture_format": texture_format,
            "audio_format": audio_format,
        })

    def export_folder(self, vfs_path: str, output_dir: str,
                      formats: Optional[dict[str, bool]] = None,
                      texture_format: str = "png", audio_format: str = "wav") -> None:
        """Export all assets under a VFS folder."""
        self._send({
            "cmd": "export_folder",
            "path": vfs_path,
            "output_dir": output_dir,
            "formats": formats or {"mesh": True, "texture": True, "props": True,
                                   "animation": True, "audio": True, "video": True},
            "texture_format": texture_format,
            "audio_format": audio_format,
        })

    def cancel(self) -> None:
        """Cancel the current export operation."""
        self._send({"cmd": "cancel"})

    def get_props(self, vfs_path: str) -> None:
        """Request full JSON properties for a uasset file."""
        self._send({"cmd": "get_props", "path": vfs_path})

    def scan_wwise_events(self) -> None:
        """Scan AkAudioEvent assets in WWiseAudio/Events for media mappings."""
        self._send({"cmd": "scan_wwise_events"})

    def scan_types(self) -> None:
        """Bulk-scan every package's export types for the type cache.

        Streams `types_scan_progress` and `types_scan_batch` messages; the
        final batch carries `final=True` and triggers `types_scan_done`.
        """
        self._send({"cmd": "scan_types"})

    def list_exports(self, vfs_path: str) -> None:
        """Request the list of exports inside a package (.upk, .uasset, .umap)."""
        self._send({"cmd": "list_exports", "path": vfs_path})

    def export_wwise_audio(self, entries: list[dict], output_dir: str,
                           audio_format: str = "wav") -> None:
        """Export and convert WWise audio files with proper naming."""
        self._send({
            "cmd": "export_wwise_audio",
            "output_dir": output_dir,
            "audio_format": audio_format,
            "entries": entries,
        })

    def export_video(self, entries: list[dict], output_dir: str) -> None:
        """Export video assets — FileMediaSource UObjects or raw video leaves.

        Each entry must carry ``vfs_path`` plus a ``kind`` of either
        ``"file_media_source"`` (resolve UObject → embedded FilePath → bytes)
        or ``"raw_video"`` (TrySaveAsset on the leaf path directly). The CLI
        replies with the same ``export_done`` payload other export commands
        produce.
        """
        self._send({
            "cmd": "export_video",
            "output_dir": output_dir,
            "entries": entries,
        })

    # ------------------------------------------------------------------
    # IPC internals
    # ------------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        if not self.is_running:
            self.error.emit("CUE4ParseCLI is not running")
            return
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._proc.write(line.encode("utf-8"))

    @Slot()
    def _on_stdout(self):
        data = self._proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._buffer += data
        # If the child sends a runaway line (no newline, megabytes long), kill
        # it rather than letting buffer growth turn into an OOM.
        if len(self._buffer) > _MAX_NDJSON_LINE_BYTES:
            log.error(
                "CUE4ParseCLI exceeded NDJSON line size (%d bytes); killing child",
                len(self._buffer),
            )
            self._buffer = ""
            try:
                if self._proc is not None:
                    self._proc.kill()
            except Exception:  # noqa: BLE001 — best-effort kill
                pass
            self.error.emit("CUE4ParseCLI emitted an oversized line; aborting.")
            return
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Non-JSON stdout from CLI: %s", line[:200])
                continue
            self._dispatch(msg)

    @Slot()
    def _on_stderr(self):
        data = self._proc.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            if line.strip():
                log.debug("CUE4ParseCLI stderr: %s", line.strip())

    @Slot(int, QProcess.ExitStatus)
    def _on_finished(self, exit_code, exit_status):
        log.info("CUE4ParseCLI exited with code %d", exit_code)
        self.process_ended.emit()

    @Slot(QProcess.ProcessError)
    def _on_error(self, err):
        msg = f"CUE4ParseCLI process error: {err}"
        log.error(msg)
        self.error.emit(msg)

    def _dispatch(self, msg: dict):
        """Route an incoming NDJSON message to the appropriate signal."""
        msg_type = msg.get("type", "")

        if msg_type == "init_done":
            self.initialized.emit(
                msg.get("archive_count", 0),
                msg.get("unmounted_count", 0),
                msg.get("file_count", 0),
                msg.get("keys_submitted", 0),
                msg.get("loose_file_count", 0),
                msg.get("unmounted_archives") or [],
            )

        elif msg_type == "browse_result":
            self.browse_result.emit(
                msg.get("path", ""),
                msg.get("entries", []),
            )

        elif msg_type == "progress":
            self.progress.emit(
                msg.get("current", 0),
                msg.get("total", 0),
                msg.get("message", ""),
            )

        elif msg_type == "export_done":
            self.export_done.emit(
                msg.get("succeeded", []),
                msg.get("failed", []),
            )

        elif msg_type == "props_result":
            self.props_result.emit(
                msg.get("path", ""),
                msg.get("exports", []),
            )

        elif msg_type == "exports_listed":
            self.exports_listed.emit(
                msg.get("path", ""),
                msg.get("exports", []),
                msg.get("error", "") or "",
            )

        elif msg_type == "wwise_scan_result":
            self.wwise_scan_result.emit(msg)

        elif msg_type == "types_scan_progress":
            self.types_scan_progress.emit(
                msg.get("current", 0),
                msg.get("total", 0),
            )

        elif msg_type == "types_scan_batch":
            entries = msg.get("entries", []) or []
            is_final = bool(msg.get("final"))
            self.types_scan_batch.emit(entries, is_final)
            if is_final:
                self.types_scan_done.emit(
                    int(msg.get("error_count", 0)),
                    int(msg.get("total_packages", 0)),
                )

        elif msg_type == "warning":
            self.warning.emit(msg.get("message", ""))

        elif msg_type == "version_warning":
            self.version_warning.emit(
                msg.get("message", ""),
                msg.get("current_version", ""),
            )

        elif msg_type == "version_detected":
            self.version_detected.emit(
                msg.get("suggested") or "",
                msg.get("source_exe") or "",
                msg.get("file_version") or msg.get("reason") or "",
            )

        elif msg_type == "error":
            self.error.emit(msg.get("message", ""))

        elif msg_type == "cancelled":
            log.info("Export cancelled by user")

        elif msg_type == "quit_ack":
            pass  # Expected during shutdown

        else:
            log.warning("Unknown message type from CLI: %s", msg_type)
