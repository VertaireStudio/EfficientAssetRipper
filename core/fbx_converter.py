"""Batch-convert exported PSKs to FBX in headless Blender.

Runs one Blender subprocess per asset (sequential) via
``core.blender_runner.run_blender`` — the same runner the .blend pipeline
uses — but with ``output_path = "...fbx"`` so the Blender script exports
the imported PSK rig directly instead of saving a .blend file.

This is the FBX export path chosen in Settings (mesh format "fbx"): the
CUE4Parse CLI writes PSKs (it has no FBX writer), and this worker turns
each new PSK into a geometry FBX afterwards.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.blender_runner import BlenderResult, run_blender


class FbxConversionWorker(QThread):
    """Convert a list of PSK paths to .fbx with headless Blender.

    One Blender process runs at a time; ``cancel()`` flips the flag and
    terminates the live subprocess immediately (mirroring JobManager) so a
    cancelled batch never leaves an orphan Blender behind.
    """

    log_message = Signal(str, str)          # (message, level)
    batch_progress = Signal(int, int, str)  # (index, total, asset name)
    completed = Signal(int, int, bool)      # (succeeded, failed, cancelled)

    def __init__(self, psk_paths: list[Path], blender_exe: str,
                 addon_name: str, timeout: int = 120, parent=None):
        super().__init__(parent)
        self._psks = list(psk_paths)
        self._blender_exe = blender_exe
        self._addon_name = addon_name
        self._timeout = timeout
        self._cancelled = False
        self._active_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    def cancel(self):
        self._cancelled = True
        with self._proc_lock:
            proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self):
        total = len(self._psks)
        if total == 0:
            self.completed.emit(0, 0, False)
            return

        succeeded = 0
        failed = 0
        self.log_message.emit(
            f"Converting {total} PSK file(s) to FBX via Blender...", "info")

        for idx, psk in enumerate(self._psks):
            if self._cancelled:
                self.log_message.emit("FBX conversion cancelled", "warning")
                break

            self.batch_progress.emit(idx, total, psk.name)
            self.log_message.emit(
                f"[{idx + 1}/{total}] FBX: {psk.name}", "info")

            manifest = {
                "psk_path": str(psk),
                "output_path": str(psk.with_suffix(".fbx")),
                "addon_name": self._addon_name,
            }

            def on_proc_started(proc, _self=self):
                with _self._proc_lock:
                    _self._active_proc = proc

            try:
                result = run_blender(
                    blender_exe=self._blender_exe,
                    manifest=manifest,
                    timeout=self._timeout,
                    cancel_check=lambda: self._cancelled,
                    on_proc_started=on_proc_started,
                )
            finally:
                with self._proc_lock:
                    self._active_proc = None

            self._log_result(result)

            if result.success:
                succeeded += 1
            else:
                failed += 1

        self.completed.emit(succeeded, failed, self._cancelled)

    def _log_result(self, result: BlenderResult):
        if result.success:
            self.log_message.emit(f"FBX: {result.asset_name} — done", "success")
        else:
            msg = f"FBX: {result.asset_name} — failed: {result.error_message or 'unknown error'}"
            self.log_message.emit(msg, "warning")
