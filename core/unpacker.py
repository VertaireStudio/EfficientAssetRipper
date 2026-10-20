"""Manages the CUE4ParseCLI subprocess and exposes Qt signals for the GUI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QTimer, Signal, Slot

from core.tools_detect import cue4parse_cli_name

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
    gpu_status = Signal(str, bool, str)       # device, available, reason — reported at init
    info = Signal(str)                        # informational CLI message (e.g. memory policy)
    browse_result = Signal(str, list)         # path, list[dict{name, is_folder}]
    progress = Signal(int, int, str)          # current, total, message
    export_done = Signal(list, list, bool, int, int)  # succeeded, failed, cancelled, remaining, warning_count
    props_result = Signal(str, list)          # path, list[dict{name, export_type, properties}]
    exports_listed = Signal(str, list, str)   # path, list[dict{name, export_type, outer}], error ("" if none)
    wwise_scan_result = Signal(dict)          # full scan result dict
    fmod_scan_result = Signal(dict)           # full FMOD bank scan result dict
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
        # GC mode of the currently running CLI ("server" until launched with
        # DOTNET_gcServer=0). Lets the panel detect a memory-settings drift
        # and remount with the correct mode.
        self.started_gc_mode = "server"

        # Cancel accounting. ``_outstanding_exports`` counts the export legs
        # the CLI still owes an ``export_done`` for (pipeline of one export
        # command per selection). Cancel does not wait for a CLI response:
        # with a leg in flight the process is shut down immediately
        # (terminate → short bounded kill), because the CLI's cancel token
        # is only observed at asset boundaries and inside its memory gate —
        # a leg wedged inside a native allocation would never poll it.
        self._outstanding_exports = 0
        self._cancel_kill_delay_ms = 2_000  # terminate → kill bound (ms)
        self._cancel_timer: QTimer | None = None
        self._cancel_shutdown_requested = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, cli_path: str, env_overrides: Optional[dict[str, str]] = None) -> bool:
        """Launch the CUE4ParseCLI subprocess.

        *env_overrides* vars are applied on top of the inherited environment
        before the process starts. The GC type is read by the .NET runtime
        (net9+: env vars take precedence over the app's runtimeconfig), so the
        memory policy's "physical only" mode switches the CLI to workstation
        GC via ``DOTNET_gcServer=0`` here — see UnpackerPanel._mount_archives.
        """
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            log.warning("CUE4ParseCLI already running")
            return True

        exe = Path(cli_path)
        if not exe.is_file():
            self.error.emit(f"CUE4ParseCLI not found: {cli_path}")
            return False

        # NixOS: the published CLI is a self-contained ELF that expects the
        # FHS dynamic loader (/lib64/ld-linux-x86-64.so.2), which only exists
        # when the user enabled nix-ld. Without it, fall back to steam-run
        # (FHS env), and fail with an actionable hint otherwise.
        launcher_args: list[str] = []
        if exe.name == cue4parse_cli_name():
            try:
                from core import platform_detect
                if platform_detect.is_nixos() and not platform_detect.has_nix_ld():
                    steam_run = platform_detect.steam_run_path()
                    if steam_run:
                        log.info("NixOS without nix-ld: launching CUE4ParseCLI via %s", steam_run)
                        (program, launcher_args) = (steam_run, [str(exe)])
                    else:
                        self.error.emit(
                            "CUE4ParseCLI cannot start on NixOS: enable nix-ld"
                            " (programs.nix-ld.enable = true; in configuration.nix)"
                            " or install Steam (provides steam-run), then try again."
                        )
                        return False
                else:
                    program = str(exe)
            except Exception:
                log.exception("platform detection failed")
                program = str(exe)
        else:
            program = str(exe)

        self._proc = QProcess(self)
        self._proc.setProgram(program)
        if launcher_args:
            self._proc.setArguments(launcher_args)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        if env_overrides:
            from PySide6.QtCore import QProcessEnvironment
            env = QProcessEnvironment.systemEnvironment()
            for key, value in env_overrides.items():
                env.insert(key, str(value))
            self._proc.setProcessEnvironment(env)
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
        self._cancel_shutdown_requested = False
        self.started_gc_mode = "workstation" if (env_overrides or {}).get("DOTNET_gcServer") == "0" else "server"
        log.info("CUE4ParseCLI started (%s GC): %s", self.started_gc_mode, cli_path)
        self.process_ready.emit()
        return True

    def stop(self):
        """Gracefully shut down the subprocess."""
        self._disarm_cancel_escalation()
        self._outstanding_exports = 0
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
                   mappings_path: str = "", gpu: Optional[bool] = None) -> None:
        """Send init command to mount game archives.

        *gpu* (optional bool) enables/disables GPU texture decoding; when
        None the CLI default (on, with per-texture CPU fallback) applies and
        the CLI still probes/reports device availability via the
        ``gpu_status`` NDJSON record.
        """
        out: dict = {
            "cmd": "init",
            "game_dir": game_dir,
            "aes_keys": aes_keys,
            "ue_version": ue_version,
            "mappings_path": mappings_path,
        }
        if gpu is not None:
            out["gpu"] = bool(gpu)
        self._send(out)

    def browse(self, vfs_path: str = "/") -> None:
        """Request directory listing at *vfs_path*."""
        self._send({"cmd": "browse", "path": vfs_path})

    def detect_ue_version(self, game_dir: str) -> None:
        """Auto-detect UE version from game .exe FileVersionInfo."""
        self._send({"cmd": "detect_ue_version", "game_dir": game_dir})

    def export(self, asset_paths: list[str], output_dir: str,
               formats: Optional[dict[str, bool]] = None,
               texture_format: str = "png", audio_format: str = "wav",
               mesh_format: str = "psk", webp_mode: str = "lossless",
               webp_quality: int = 90,
               threads: Optional[int] = None, gpu: Optional[bool] = None,
               memory_limit_percent: int = 0, physical_only: bool = False) -> None:
        """Export specific assets.

        *gpu* (optional bool) enables/disables GPU texture decoding for this
        export batch; None keeps the CLI default (on, CPU fallback).
        *mesh_format* picks the mesh output ("psk" or "gltf"); *webp_mode* /
        *webp_quality* apply only when *texture_format* is "webp".
        *memory_limit_percent* (0 = unlimited, 10-95) caps the exporter at a
        share of the RAM currently free; *physical_only* additionally enforces
        that the working set stays inside physical memory (see Settings ·
        Memory).
        """
        cmd: dict = {
            "cmd": "export",
            "paths": asset_paths,
            "output_dir": output_dir,
            "formats": formats or {"mesh": True, "texture": True, "props": True,
                                   "animation": True, "audio": True, "video": True},
            "texture_format": texture_format,
            "audio_format": audio_format,
            "mesh_format": mesh_format,
            "webp_mode": webp_mode,
            "webp_quality": webp_quality,
        }
        if threads is not None:
            cmd["threads"] = threads
        if gpu is not None:
            cmd["gpu"] = bool(gpu)
        self._append_memory_fields(cmd, memory_limit_percent, physical_only)
        self._send(cmd)

    def export_folder(self, vfs_path: str, output_dir: str,
                      formats: Optional[dict[str, bool]] = None,
                      texture_format: str = "png", audio_format: str = "wav",
                      mesh_format: str = "psk", webp_mode: str = "lossless",
                      webp_quality: int = 90,
                      threads: Optional[int] = None, gpu: Optional[bool] = None,
                      memory_limit_percent: int = 0, physical_only: bool = False) -> None:
        """Export all assets under a VFS folder.

        *gpu* (optional bool) enables/disables GPU texture decoding for this
        export batch; None keeps the CLI default (on, CPU fallback).
        *mesh_format* picks the mesh output ("psk" or "gltf"); *webp_mode* /
        *webp_quality* apply only when *texture_format* is "webp".
        *memory_limit_percent* (0 = unlimited, 10-95) caps the exporter at a
        share of the RAM currently free; *physical_only* additionally enforces
        that the working set stays inside physical memory (see Settings ·
        Memory).
        """
        cmd: dict = {
            "cmd": "export_folder",
            "path": vfs_path,
            "output_dir": output_dir,
            "formats": formats or {"mesh": True, "texture": True, "props": True,
                                   "animation": True, "audio": True, "video": True},
            "texture_format": texture_format,
            "audio_format": audio_format,
            "mesh_format": mesh_format,
            "webp_mode": webp_mode,
            "webp_quality": webp_quality,
        }
        if threads is not None:
            cmd["threads"] = threads
        if gpu is not None:
            cmd["gpu"] = bool(gpu)
        self._append_memory_fields(cmd, memory_limit_percent, physical_only)
        self._send(cmd)

    @staticmethod
    def _append_memory_fields(cmd: dict, memory_limit_percent: int,
                              physical_only: bool) -> None:
        """Add the memory-policy fields to an export command, but only when
        active — unset policy follows the same "omit the field" convention as
        ``threads``/``gpu`` so older CLIs see no behavior change."""
        if memory_limit_percent and memory_limit_percent > 0:
            cmd["memory_limit_percent"] = int(memory_limit_percent)
        if physical_only:
            cmd["physical_only"] = True

    def cancel(self) -> None:
        """Cancel the current export operation.

        A cancel does not wait for the CLI to respond: when an export leg is
        in flight the CLI process is shut down immediately (terminate,
        followed by a short bounded kill if the graceful stop is ignored).
        The session ends with the export — re-mount for further work — and
        the panel reports the interrupted run. Idle cancels (no leg
        outstanding, e.g. a click between legs) still send the graceful
        cancel token, which the CLI observes at its next boundary without
        ending the session.
        """
        self._disarm_cancel_escalation()
        proc = self._proc
        if proc is None or self._outstanding_exports <= 0:
            # Idle corner — nothing to tear down; the token is honored
            # (and acked) at the CLI's next boundary.
            self._send({"cmd": "cancel"})
            return
        log.warning("Cancel during export — shutting CUE4ParseCLI down immediately")
        self.warning.emit(
            "Export cancelled — shutting the export process down immediately."
        )
        self._cancel_shutdown_requested = True
        self._outstanding_exports = 0
        proc.terminate()
        # Graceful shutdown has a short bound: a leg wedged inside native
        # code ignores SIGTERM/WM_CLOSE exactly like the old ladder assumed.
        timer = QTimer(self)  # parented → dies with this QObject
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_cancel_kill_timeout)
        self._cancel_timer = timer
        timer.start(self._cancel_kill_delay_ms)

    # ------------------------------------------------------------------
    # Cancel teardown: immediate shutdown with a bounded kill fallback
    # ------------------------------------------------------------------

    @property
    def cancel_shutdown_requested(self) -> bool:
        """True when the last cancel tore the CLI down immediately (so the
        panel can word the interrupted-export note as user-acted, not a
        crash)."""
        return self._cancel_shutdown_requested

    def _disarm_cancel_escalation(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer.stop()
            self._cancel_timer.deleteLater()
            self._cancel_timer = None

    def _on_cancel_kill_timeout(self) -> None:
        """The graceful shutdown set on cancel was ignored — kill hard."""
        if not self.is_running:
            return
        try:
            log.warning("Cancel fallback: killing CUE4ParseCLI")
            if self._proc is not None:
                self._proc.kill()
        except Exception:  # noqa: BLE001 — teardown must never raise
            log.exception("Cancel kill fallback failed")

    def _on_export_report(self) -> None:
        """One export leg finished — account it and disarm the ladder."""
        self._outstanding_exports = max(0, self._outstanding_exports - 1)
        self._disarm_cancel_escalation()

    def get_props(self, vfs_path: str) -> None:
        """Request full JSON properties for a uasset file."""
        self._send({"cmd": "get_props", "path": vfs_path})

    def scan_wwise_events(self) -> None:
        """Scan AkAudioEvent assets in WWiseAudio/Events for media mappings."""
        self._send({"cmd": "scan_wwise_events"})

    def scan_fmod_banks(self) -> None:
        """Scan FMOD .bank files (VFS + loose) for playable streams."""
        self._send({"cmd": "scan_fmod_banks"})

    def export_fmod_audio(self, entries: list[dict], output_dir: str,
                          threads: Optional[int] = None,
                          memory_limit_percent: int = 0,
                          physical_only: bool = False) -> None:
        """Export individual FMOD bank streams to WAV.

        Each entry carries ``bank_vfs_path``, ``stream_index`` (1-based,
        vgmstream subsong numbering), ``target_name`` and ``target_folder``.
        *threads* (optional) sets the CLI worker count for the parallel
        vgmstream conversions; None lets the CLI default run one worker per
        core.
        """
        cmd: dict = {
            "cmd": "export_fmod_audio",
            "output_dir": output_dir,
            "entries": entries,
        }
        if threads is not None and threads > 0:
            cmd["threads"] = int(threads)
        self._append_memory_fields(cmd, memory_limit_percent, physical_only)
        self._send(cmd)

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
                           audio_format: str = "wav",
                           threads: Optional[int] = None,
                           memory_limit_percent: int = 0,
                           physical_only: bool = False) -> None:
        """Export and convert WWise audio files with proper naming.

        *threads* (optional) sets the CLI worker count for the parallel
        extract-and-convert workers; None lets the CLI default run one
        worker per core.
        """
        cmd: dict = {
            "cmd": "export_wwise_audio",
            "output_dir": output_dir,
            "audio_format": audio_format,
            "entries": entries,
        }
        if threads is not None and threads > 0:
            cmd["threads"] = int(threads)
        self._append_memory_fields(cmd, memory_limit_percent, physical_only)
        self._send(cmd)

    def export_video(self, entries: list[dict], output_dir: str,
                     memory_limit_percent: int = 0,
                     physical_only: bool = False) -> None:
        """Export video assets — FileMediaSource UObjects or raw video leaves.

        Each entry must carry ``vfs_path`` plus a ``kind`` of either
        ``"file_media_source"`` (resolve UObject → embedded FilePath → bytes)
        or ``"raw_video"`` (TrySaveAsset on the leaf path directly). The CLI
        replies with the same ``export_done`` payload other export commands
        produce.
        """
        cmd: dict = {
            "cmd": "export_video",
            "output_dir": output_dir,
            "entries": entries,
        }
        self._append_memory_fields(cmd, memory_limit_percent, physical_only)
        self._send(cmd)

    # ------------------------------------------------------------------
    # IPC internals
    # ------------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        if not self.is_running:
            self.error.emit("CUE4ParseCLI is not running")
            return
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._proc.write(line.encode("utf-8"))
        # Every export flavor ("export", "export_folder", "export_*_audio",
        # "export_video") posts exactly one export_done when its leg drains —
        # count it so cancel only tears the CLI down for live legs.
        if str(obj.get("cmd", "")).startswith("export"):
            self._outstanding_exports += 1

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
        self._disarm_cancel_escalation()
        self._outstanding_exports = 0
        self.process_ended.emit()
        # Clear after the emit: the panel's direct-connected slot reads the
        # flag synchronously during process_ended to word its abort note.
        self._cancel_shutdown_requested = False

    @Slot(QProcess.ProcessError)
    def _on_error(self, err):
        if self._cancel_shutdown_requested:
            # Our own cancel teardown just killed the process, which QProcess
            # reports as ProcessError.Crashed — expected, not a failure. The
            # panel already notes the cancelled export via process_ended, so
            # a synthetic "process crashed" error here would only alarm.
            log.info("CUE4ParseCLI process errored (%s) after cancel shutdown", err)
            return
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

        elif msg_type == "info":
            self.info.emit(msg.get("message", ""))

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
            self._on_export_report()
            self.export_done.emit(
                msg.get("succeeded", []),
                msg.get("failed", []),
                bool(msg.get("cancelled", False)),
                int(msg.get("remaining", 0)),
                int(msg.get("warning_count", 0)),
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

        elif msg_type == "fmod_scan_result":
            self.fmod_scan_result.emit(msg)

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

        elif msg_type == "gpu_status":
            self.gpu_status.emit(
                msg.get("device", ""),
                bool(msg.get("available", False)),
                msg.get("reason", "") or "",
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
            # The CLI acks Cancel instantly while the export thread is still
            # draining — or wedged. `exporting` (added CLI-side) says whether
            # a leg actually survived; idle cancels reset the count to zero,
            # so the escalation ladder can never arm for them.
            self._outstanding_exports = 1 if bool(msg.get("exporting")) else 0
            if self._outstanding_exports == 0:
                self._disarm_cancel_escalation()

        elif msg_type == "quit_ack":
            pass  # Expected during shutdown

        else:
            log.warning("Unknown message type from CLI: %s", msg_type)
