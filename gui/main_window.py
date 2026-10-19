"""Main application window — ties together all panels."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QRunnable, QThread, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStatusBar,
    QWidget,
)

import config
from _version import __version__
from core.asset_scanner import (
    AssetEntry,
    AssetScanner,
    load_scan_cache,
    save_scan_cache,
    sweep_old_cache_backups,
)
from core.everything import EverythingError, get_sdk, reset_sdk
from core.job_manager import JobManager
from core.profile_manager import ProfileLoadError, ProfileManager
from gui.asset_browser import AssetBrowser
from gui.blend_combiner import BlendCombinerPanel
from gui.tga_previewer import TGAPreviewerPanel
from gui.media_previewer import MediaPreviewerPanel
from gui.mesh_previewer import MeshPreviewerPanel
from gui.log_viewer import LogViewer
from gui.psk_picker import PskPickerPanel
from gui.queue_panel import QueuePanel
from gui.text_viewer import TextViewer
from gui.unpacker_panel import UnpackerPanel
from gui.settings_panel import SettingsDialog
import gui.theme as theme

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scanner worker (runs in background thread)
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    """Background thread for asset scanning."""

    progress = Signal(int, int, str)
    finished = Signal(list)         # list[AssetEntry]
    error = Signal(str)

    def __init__(self, scanner: AssetScanner, parent=None):
        super().__init__(parent)
        self._scanner = scanner

    def cancel(self):
        self._scanner.cancel()
        self.requestInterruption()

    def run(self):
        try:
            results = self._scanner.scan(
                progress_callback=lambda cur, tot, msg: self.progress.emit(cur, tot, msg)
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class RescanWorker(QThread):
    """Background thread for re-resolving individual assets."""

    progress = Signal(int, int, str)
    finished = Signal(int, int)     # (resolved_count, still_incomplete)
    error = Signal(str)

    def __init__(self, scanner: AssetScanner, entries: list, parent=None):
        super().__init__(parent)
        self._scanner = scanner
        self._entries = entries
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self._scanner.cancel()
        self.requestInterruption()

    def run(self):
        try:
            total = len(self._entries)
            resolved = 0
            for idx, entry in enumerate(self._entries):
                if self._cancelled or self.isInterruptionRequested():
                    self.progress.emit(idx, total, "Re-scan cancelled")
                    break
                self.progress.emit(idx, total, f"Re-scanning: {entry.name}")
                self._scanner.resolve_entry(entry)
                if entry.status == "ready":
                    resolved += 1
            self.progress.emit(total, total, "Re-scan complete")
            still_incomplete = total - resolved
            self.finished.emit(resolved, still_incomplete)
        except Exception as e:
            self.error.emit(str(e))


class _PickerResolveWorker(QThread):
    """Background thread for resolving PSK entries from the picker."""

    progress = Signal(int, int, str)
    entry_resolved = Signal(object)  # emits each AssetEntry immediately after resolution
    finished = Signal(int, int)     # (resolved_count, still_incomplete)
    error = Signal(str)

    def __init__(self, scanner: AssetScanner, entries: list, parent=None):
        super().__init__(parent)
        self._scanner = scanner
        self._entries = entries
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self._scanner.cancel()
        self.requestInterruption()

    def run(self):
        try:
            total = len(self._entries)
            resolved = 0
            for idx, entry in enumerate(self._entries):
                if self._cancelled or self.isInterruptionRequested():
                    self.progress.emit(idx, total, "Resolve cancelled")
                    break
                self.progress.emit(idx, total, f"Resolving: {entry.name}")
                self._scanner.resolve_entry(entry)
                if entry.status == "ready":
                    resolved += 1
                self.entry_resolved.emit(entry)
            self.progress.emit(total, total, "Resolve complete")
            still_incomplete = total - resolved
            self.finished.emit(resolved, still_incomplete)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Cache write runnable — moves disk I/O off the GUI thread
# ---------------------------------------------------------------------------

class _CacheWriteRunnable(QRunnable):
    """Submits save_scan_cache work to QThreadPool."""

    def __init__(self, assets: list, game_folder: str, on_done=None):
        super().__init__()
        self._assets = list(assets)
        self._game_folder = game_folder
        self._on_done = on_done

    def run(self):
        try:
            save_scan_cache(self._assets, self._game_folder)
        except Exception:
            log.exception("Async cache write failed")
        finally:
            if self._on_done is not None:
                try:
                    self._on_done()
                except Exception:
                    log.exception("Cache write completion callback failed")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"EfficientAssetRipper v{__version__}")
        self.setMinimumSize(1200, 700)

        self._job_manager: JobManager | None = None
        self._current_profile_name = ""
        # All long-running QThread workers register here on creation and
        # auto-prune themselves on `finished`. closeEvent walks the list to
        # make sure nothing keeps spinning past window destruction.
        self._active_workers: list[QThread] = []
        # Outstanding cache writes — closeEvent waits on this counter.
        self._pending_cache_writes = 0

        # Profile manager
        self._profile_manager = ProfileManager()
        self._profile_manager.migrate_from_qsettings(config)

        self._build_ui()
        self._build_menu()
        self._refresh_blender_dependent_ui()
        # Restore the user's last dock layout + window geometry. Falls back
        # to the default factory layout on schema mismatch / first launch /
        # corrupt state.
        if not self._restore_layout():
            self._apply_default_geometry()
        self._load_initial_profile()
        # Defer the setup wizard until after splash → window handoff so the
        # modal dialog doesn't fight the splash for focus.
        QTimer.singleShot(0, self._maybe_run_setup_wizard)
        # Same deferral for the resume prompt — keeps focus order sane and
        # gives the wizard precedence on first launch.
        QTimer.singleShot(0, self._maybe_prompt_resume)

        # Best-effort startup housekeeping. Old scan-cache backups left behind
        # by version-bump renames pile up otherwise.
        try:
            sweep_old_cache_backups()
        except Exception:
            log.exception("sweep_old_cache_backups failed (non-fatal)")

        # Kick off the auto-update check 2s after launch so it never delays
        # the splash. UpdateChecker fails silently on any error.
        self._update_info = None
        self._update_checker = None
        self._manual_update_checker = None
        QTimer.singleShot(2000, self._start_update_check)

    # ------------------------------------------------------------------
    # Worker tracking
    # ------------------------------------------------------------------

    def _track_worker(self, worker: QThread) -> None:
        """Register a worker thread for shutdown tracking + auto-cleanup."""
        self._active_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.finished.connect(worker.deleteLater)

    def _on_worker_finished(self, worker: QThread) -> None:
        try:
            self._active_workers.remove(worker)
        except ValueError:
            pass

    def _on_fbx_requested(self, psks: list):
        """Convert newly exported PSKs to .fbx with headless Blender.

        Runs one Blender process per asset sequentially in a tracked worker
        thread; cancel-safe (process tree terminated on cancel/shutdown).
        """
        from core.blender_runner import is_blender_available
        from core.fbx_converter import FbxConversionWorker

        if not is_blender_available():
            self._log.append(
                "FBX mesh export requested but Blender is not available — "
                "PSK files kept as-is", "warning")
            return

        timeout = int(config.get_int("timeout_seconds") or 120)
        worker = FbxConversionWorker(
            [Path(p) for p in psks],
            blender_exe=config.get("blender_exe") or "",
            addon_name=config.get("psk_addon_name")
                or "bl_ext.blender_org.io_scene_psk_psa",
            timeout=timeout,
        )
        worker.log_message.connect(self._log.append)
        worker.completed.connect(self._on_fbx_completed)
        self._track_worker(worker)
        worker.start()

    def _on_fbx_completed(self, succeeded: int, failed: int, cancelled: bool):
        if cancelled:
            msg = f"FBX conversion cancelled: {succeeded} ok, {failed} failed"
            self._log.append(msg, "warning")
        else:
            msg = f"FBX conversion: {succeeded} ok, {failed} failed"
            self._log.append(msg, "success" if failed == 0 else "warning")
        self._statusbar.showMessage(msg)

    def _maybe_run_setup_wizard(self):
        """Show the first-run wizard if it hasn't been completed yet."""
        from gui.setup_wizard import SetupWizard, should_show_setup
        if not should_show_setup():
            return
        wizard = SetupWizard(self)
        wizard.exec()
        if getattr(wizard, "open_profile_manager_after", False):
            self._open_profile_dialog()

    def _force_run_setup_wizard(self):
        """Help-menu trigger: re-fire the wizard for an existing user.

        We do NOT clear ``setup_complete`` here — the wizard reads that flag
        at construction to decide whether to render the first-run "set up
        your first profile" page or the re-run "go to the Profiles menu"
        page. Clearing it would always look like a first run.
        """
        from gui.setup_wizard import SetupWizard
        wizard = SetupWizard(self)
        wizard.exec()
        if getattr(wizard, "open_profile_manager_after", False):
            self._open_profile_dialog()

    def _start_update_check(self):
        from core.update_check import UpdateChecker
        self._update_checker = UpdateChecker(parent=self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.start()

    @Slot(object)
    def _on_update_available(self, info):
        self._update_info = info
        self._statusbar.showMessage(
            f"Update available: {info.latest} (you have v{info.current}) — Help → About",
            10_000,
        )
        log.info("Update available: %s (current %s)", info.latest, info.current)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Central widget: empty placeholder ─────────────────────────
        # Every panel ships as a QDockWidget; QMainWindow requires a
        # central widget but ours has no chrome of its own. Profile
        # selection moved to the Profiles menu, scan controls moved into
        # the Asset Browser dock.
        central = QWidget()
        self.setCentralWidget(central)
        central.setMaximumSize(0, 0)  # collapse to zero so docks meet in the middle

        # Tracks the currently active profile so menu rebuilds can mark it
        # checked. Also used by busy-check / revert flow for menu-driven
        # profile switches.
        self._active_profile_name: str = ""
        self._suppress_profile_menu_event: bool = False

        # ── Build panels ──────────────────────────────────────────────
        self._browser = AssetBrowser()
        self._psk_picker = PskPickerPanel()
        self._unpacker_panel = UnpackerPanel()

        self._queue = QueuePanel()
        self._log = LogViewer()

        self._combiner = BlendCombinerPanel()
        self._combiner.log_message.connect(self._log.append)
        self._tga_previewer = TGAPreviewerPanel()
        self._text_viewer = TextViewer()
        self._media_previewer = MediaPreviewerPanel()
        self._mesh_previewer = MeshPreviewerPanel()

        # ── Wrap panels in QDockWidgets (in-window only — no floating) ─
        # `_dock_specs` is the single source of truth for dock identity and
        # the Window menu ordering; placement lives in
        # `_apply_default_dock_layout`.
        self._dock_specs: list[tuple[str, str, str, QWidget]] = [
            # (object_name,            menu label,        title,             widget)
            ("dock_unpacker",         "Unpacker",        "Unpacker",        self._unpacker_panel),
            ("dock_psk_picker",       "PSK Picker",      "PSK Picker",      self._psk_picker),
            ("dock_asset_browser",    "Asset Browser",   "Asset Browser",   self._browser),
            ("dock_queue",            "Queue",           "Queue",           self._queue),
            ("dock_log",              "Log",             "Log",             self._log),
            ("dock_blend_combiner",   "Blend Combiner",  "Blend Combiner",  self._combiner),
            ("dock_tga_previewer",    "TGA Previewer",   "TGA Previewer",   self._tga_previewer),
            ("dock_mesh_previewer",   "Mesh Preview",    "Mesh Preview",    self._mesh_previewer),
            ("dock_text_viewer",      "Text Viewer",     "Text Viewer",     self._text_viewer),
            ("dock_media_previewer",  "Media Preview",   "Media Preview",   self._media_previewer),
        ]
        self._docks: dict[str, QDockWidget] = {}
        for object_name, _menu, title, widget in self._dock_specs:
            dock = QDockWidget(title, self)
            dock.setObjectName(object_name)
            dock.setWidget(widget)
            dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
            # No DockWidgetFloatable → user can't pop docks out as OS windows.
            dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QDockWidget.DockWidgetFeature.DockWidgetMovable
            )
            self._docks[object_name] = dock

        # Friendly attributes for the rest of the file (and for tests).
        self._asset_browser_dock = self._docks["dock_asset_browser"]
        self._psk_picker_dock = self._docks["dock_psk_picker"]
        self._unpacker_dock = self._docks["dock_unpacker"]
        self._queue_dock = self._docks["dock_queue"]
        self._log_dock = self._docks["dock_log"]
        self._combiner_dock = self._docks["dock_blend_combiner"]
        self._tga_previewer_dock = self._docks["dock_tga_previewer"]
        self._text_viewer_dock = self._docks["dock_text_viewer"]
        self._media_previewer_dock = self._docks["dock_media_previewer"]
        self._mesh_previewer_dock = self._docks["dock_mesh_previewer"]

        # Tabify-tabs at the TOP of each dock area (default Qt is bottom).
        # Applied to all four areas so tabs stay on top regardless of where
        # the user drags a dock.
        from PySide6.QtWidgets import QTabWidget
        for _area in (
            Qt.DockWidgetArea.LeftDockWidgetArea,
            Qt.DockWidgetArea.RightDockWidgetArea,
            Qt.DockWidgetArea.TopDockWidgetArea,
            Qt.DockWidgetArea.BottomDockWidgetArea,
        ):
            self.setTabPosition(_area, QTabWidget.TabPosition.North)

        # Allow nested dock layouts so the user can drop a dock onto an
        # edge of another to split that area further (3+ panes side-by-side
        # or in a grid, like VS Code). Without this, Qt limits to one
        # row/column per area and additional drops get tabified instead.
        self.setDockNestingEnabled(True)

        # GroupedDragging is what enables dragging a single tab OUT of a
        # tabbed dock group — Qt gates the tab-bar drag path on this dock
        # option, not on DockWidgetFloatable. Docks stay non-floatable, so
        # a drop outside any dock area re-plugs the dock instead of
        # detaching it as an OS window.
        self.setDockOptions(
            self.dockOptions() | QMainWindow.DockOption.GroupedDragging
        )

        self._apply_default_dock_layout()
        # Snapshot the default layout *once*, immediately after building it.
        # Reset Layout calls restoreState(...) with this blob instead of
        # tearing docks out one-by-one — removeDockWidget on a hidden dock
        # while the window is shown can ACV inside Qt's internals on Windows.
        self._default_layout_state = self.saveState(self._LAYOUT_SCHEMA_VERSION)

        # Status bar
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready — configure settings and scan to begin")

        # Connect signals
        self._queue.process_requested.connect(self._process_queue)
        self._queue.cancel_requested.connect(self._cancel_processing)
        self._queue.reprocess_requested.connect(self._reprocess_asset)
        self._browser.rescan_requested.connect(self._rescan_selected)
        self._browser.add_to_queue_requested.connect(self._add_browser_to_queue)
        self._browser.reprocess_requested.connect(self._reprocess_asset)
        self._browser.delete_requested.connect(self._on_browser_delete)
        self._browser.mesh_preview_requested.connect(self._on_browser_mesh_preview)
        self._browser.props_view_requested.connect(self._on_browser_props_view)
        self._browser.scan_requested.connect(self._start_scan)
        self._browser.cancel_scan_requested.connect(self._cancel_scan)
        self._psk_picker.add_to_queue_requested.connect(self._add_picker_to_queue)
        self._psk_picker.mesh_preview_requested.connect(self._on_mesh_preview)
        self._unpacker_panel.psk_extracted.connect(self._on_psks_extracted)
        self._unpacker_panel.fbx_requested.connect(self._on_fbx_requested)
        self._unpacker_panel.log_message.connect(self._log.append)
        self._unpacker_panel.version_mismatch.connect(self._log.show_alert)
        self._unpacker_panel.props_viewed.connect(self._show_in_text_viewer)
        self._unpacker_panel.media_preview.connect(self._on_media_preview)
        self._unpacker_panel.tga_preview.connect(self._on_tga_preview)
        self._unpacker_panel.mesh_preview.connect(self._on_mesh_preview)
        self._unpacker_panel.aes_keys_required.connect(self._on_aes_keys_required)

        # Give the unpacker access to each previewer's temp directory so it
        # can drop preview-only exports there instead of the user's real
        # output folder.
        self._unpacker_panel._media_preview_temp_dir = self._media_previewer.temp_dir
        self._unpacker_panel._mesh_preview_temp_dir = self._mesh_previewer.temp_dir
        self._unpacker_panel._tga_preview_temp_dir = self._tga_previewer.temp_dir

    def _show_in_text_viewer(self, title: str, text: str):
        """Display text content in the Text Viewer dock and surface it."""
        self._text_viewer.show_text(title, text)
        self._raise_dock(self._text_viewer_dock)

    def _on_media_preview(self, path: str):
        """Load audio or video file in the Media Preview dock and surface it."""
        self._media_previewer.load_file(path)
        self._raise_dock(self._media_previewer_dock)

    def _on_tga_preview(self, path: str):
        """Load image file in the TGA Previewer dock and surface it."""
        self._tga_previewer.load_file(path)
        self._raise_dock(self._tga_previewer_dock)

    def _on_mesh_preview(self, path: str):
        """Load .psk in the Mesh Preview dock and surface it.

        glTF (.glb) / FBX exports preview externally: the built-in viewer is
        a PSK parser, so anything else shows a hint instead of a parse error.
        """
        if not path.lower().endswith((".psk", ".pskx")):
            self._log.append(
                f"{Path(path).name}: glTF/FBX previews open in the system "
                "default app (the built-in mesh viewer reads PSK only)", "info")
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self._raise_dock(self._mesh_previewer_dock)
            return
        self._mesh_previewer.load_psk(path)
        self._raise_dock(self._mesh_previewer_dock)

    @staticmethod
    def _raise_dock(dock: QDockWidget) -> None:
        dock.show()
        dock.raise_()

    def _on_browser_mesh_preview(self, asset):
        self._on_mesh_preview(str(asset.psk_path))

    def _on_browser_props_view(self, asset):
        """Open the asset's .props.txt in the Text Viewer; toast if absent."""
        props = asset.psk_path.with_suffix(".props.txt")
        if not props.is_file():
            self._statusbar.showMessage(f"No .props.txt for {asset.name}", 4000)
            return
        try:
            text = props.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._statusbar.showMessage(f"Could not read props: {e}", 5000)
            return
        self._show_in_text_viewer(f"Properties — {asset.name}", text)

    def _build_menu(self):
        from PySide6.QtGui import QAction, QActionGroup

        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        settings_action = file_menu.addAction("&Settings...", self._open_settings)
        # NoRole keeps Qt from auto-promoting "Settings"/"Exit" into the
        # platform-native application menu (or doubling the entry on
        # platforms where it inserts a duplicated mirror).
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("E&xit", self.close)
        exit_action.setMenuRole(QAction.MenuRole.NoRole)

        # ── Profiles menu — replaces the in-window ProfileBar ─────────
        # Built dynamically: aboutToShow re-reads the profile manager so
        # newly-created/renamed/deleted profiles are reflected without
        # needing an explicit refresh from the dialog.
        self._profiles_menu = menubar.addMenu("&Profiles")
        self._profile_action_group = QActionGroup(self._profiles_menu)
        self._profile_action_group.setExclusive(True)
        self._profiles_menu.aboutToShow.connect(self._rebuild_profiles_menu)
        # Build once now so other code (refresh callers) can rely on it.
        self._rebuild_profiles_menu()

        tools_menu = menubar.addMenu("&Tools")
        self._blend_combiner_action = tools_menu.addAction(
            "&Blend Combiner",
            lambda: self._raise_dock(self._combiner_dock),
        )

        # ── Window menu — dock visibility (in original L→R order) +
        # Reset Layout. QDockWidget.toggleViewAction() handles the check
        # state syncing whenever the user closes a dock via its X button.
        window_menu = menubar.addMenu("&Window")
        for object_name, menu_label, _title, _widget in self._dock_specs:
            dock = self._docks[object_name]
            action = dock.toggleViewAction()
            action.setText(menu_label)
            window_menu.addAction(action)
        window_menu.addSeparator()
        reset_action = window_menu.addAction("&Reset Layout")
        reset_action.triggered.connect(self._reset_dock_layout)

        help_menu = menubar.addMenu("&Help")
        help_menu.addAction("Run Setup &Wizard...", self._force_run_setup_wizard)
        help_menu.addAction("Check for &Updates...", self._check_for_updates_now)
        help_menu.addSeparator()
        help_menu.addAction("&About...", self._show_about)

    def _show_about(self):
        from html import escape
        from urllib.parse import urlparse

        update_html = ""
        if self._update_info and self._update_info.is_newer:
            url = self._update_info.release_url or ""
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            # The update_check module already filters URLs, but defence-in-depth
            # belongs at the render site too — never embed a URL from anywhere
            # other than github.com over HTTPS.
            safe_url = url if (parsed.scheme == "https" and host == "github.com") else ""
            tag_html = escape(self._update_info.latest)
            if safe_url:
                update_html = (
                    f"<p><b>Update available:</b> {tag_html} — "
                    f'<a href="{escape(safe_url, quote=True)}">view release</a></p>'
                )
            else:
                update_html = f"<p><b>Update available:</b> {tag_html}</p>"
        QMessageBox.about(
            self,
            "About EfficientAssetRipper",
            f"<h3>EfficientAssetRipper v{__version__}</h3>"
            "<p>An asset extraction tool for Unreal Engine 4 and 5 games, "
            "built on CUE4Parse with an automated Blender export pipeline.</p>"
            "<p>Licensed under the MIT License. Not affiliated with Epic Games, Inc. "
            "Using or distributing extracted output may be against copyright "
            "legislation in your jurisdiction — you are responsible for "
            "ensuring you're not breaking any laws.</p>"
            f"{update_html}"
            '<p><a href="https://github.com/exterminathan/EfficientAssetRipper">'
            "github.com/exterminathan/EfficientAssetRipper</a></p>",
        )

    def _check_for_updates_now(self):
        """Help-menu trigger: force a fresh update check and show the result."""
        from html import escape
        from urllib.parse import urlparse

        from core.update_check import UpdateChecker

        # If a previous menu-triggered checker is still running, ignore the
        # click — the user can wait for it to complete.
        if getattr(self, "_manual_update_checker", None) is not None:
            checker = self._manual_update_checker
            worker = getattr(checker, "_worker", None)
            if worker is not None and worker.isRunning():
                return

        progress = QProgressDialog("Checking for updates...", None, 0, 0, self)
        progress.setWindowTitle("Check for Updates")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()

        checker = UpdateChecker(parent=self)
        self._manual_update_checker = checker

        def _on_complete(info):
            try:
                progress.close()
            except Exception:  # noqa: BLE001
                pass
            self._manual_update_checker = None

            if info is None:
                QMessageBox.warning(
                    self, "Check for Updates",
                    "Could not reach the update server.\n\n"
                    "Check your network connection and try again later.",
                )
                return

            # Cache the freshest result so the About dialog reflects it too.
            self._update_info = info

            if info.is_newer:
                url = info.release_url or ""
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                safe_url = url if (parsed.scheme == "https" and host == "github.com") else ""
                tag_html = escape(info.latest)
                body = (
                    f"<p><b>A new version is available: {tag_html}</b></p>"
                    f"<p>You are running v{escape(info.current)}.</p>"
                )
                if safe_url:
                    body += (
                        f'<p><a href="{escape(safe_url, quote=True)}">'
                        "View the release on GitHub</a></p>"
                    )
                QMessageBox.information(self, "Update Available", body)
            else:
                QMessageBox.information(
                    self, "Up to Date",
                    f"You are running the latest version (v{info.current}).",
                )

        checker.check_complete.connect(_on_complete)
        checker.start(force_refresh=True)

    # ------------------------------------------------------------------
    # Dock layout (default + persistence)
    # ------------------------------------------------------------------

    _LAYOUT_SCHEMA_VERSION = 4
    _LAYOUT_KEY_GEOMETRY = "ui/main_window_geometry"
    _LAYOUT_KEY_STATE = "ui/main_window_state"
    _LAYOUT_KEY_SCHEMA = "ui/main_window_layout_schema"

    def _apply_default_dock_layout(self) -> None:
        """Factory layout.

        Left area: the browser/picker/unpacker tab group and the Queue side
        by side, with the Log spanning underneath both. Right area: TGA/Mesh
        previews tabbed on top, Text/Media tabbed below.

        Only used at startup — Reset Layout uses ``restoreState`` against the
        snapshot captured right after this runs, which avoids the
        ``removeDockWidget``-on-hidden-dock crash that Qt can hit on Windows.
        """
        d = self._docks
        left = ("dock_unpacker", "dock_psk_picker", "dock_asset_browser",
                "dock_queue", "dock_blend_combiner", "dock_log")
        right = ("dock_tga_previewer", "dock_mesh_previewer",
                 "dock_text_viewer", "dock_media_previewer")
        for name in left:
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d[name])
            d[name].setVisible(True)
        for name in right:
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d[name])
            d[name].setVisible(True)

        # Log first spans the full left-area width beneath the unpacker cell,
        # then that top cell splits into main tabs | queue (nested docking).
        self.splitDockWidget(d["dock_unpacker"], d["dock_log"], Qt.Orientation.Vertical)
        self.splitDockWidget(d["dock_unpacker"], d["dock_queue"], Qt.Orientation.Horizontal)
        self.tabifyDockWidget(d["dock_unpacker"], d["dock_psk_picker"])
        self.tabifyDockWidget(d["dock_psk_picker"], d["dock_asset_browser"])
        self.tabifyDockWidget(d["dock_queue"], d["dock_blend_combiner"])

        # Right column: TGA/Mesh tabs above Text/Media tabs.
        self.splitDockWidget(d["dock_tga_previewer"], d["dock_text_viewer"], Qt.Orientation.Vertical)
        self.tabifyDockWidget(d["dock_tga_previewer"], d["dock_mesh_previewer"])
        self.tabifyDockWidget(d["dock_text_viewer"], d["dock_media_previewer"])

        d["dock_unpacker"].raise_()
        d["dock_queue"].raise_()
        d["dock_mesh_previewer"].raise_()
        d["dock_text_viewer"].raise_()

        self.resizeDocks(
            [d["dock_unpacker"], d["dock_queue"], d["dock_mesh_previewer"]],
            [600, 400, 500],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks(
            [d["dock_unpacker"], d["dock_log"]],
            [650, 220],
            Qt.Orientation.Vertical,
        )

        # Hidden by default, but positioned above so the Window menu or an
        # asset-view call reopens them in their home spot (combiner tabbed
        # with Queue, TGA top-right group, Media bottom-right group).
        for name in ("dock_blend_combiner", "dock_tga_previewer", "dock_media_previewer"):
            d[name].setVisible(False)

    def _reset_dock_layout(self) -> None:
        """Window → Reset Layout. Restores defaults and clears persisted state."""
        # Reopen any closed docks first so restoreState has them to position.
        for dock in self._docks.values():
            if not dock.isVisible():
                dock.show()
        # Restore the snapshot captured right after the initial layout —
        # this is the canonical "factory" state.
        if getattr(self, "_default_layout_state", None) is not None:
            self.restoreState(self._default_layout_state, self._LAYOUT_SCHEMA_VERSION)
        try:
            config.set(self._LAYOUT_KEY_GEOMETRY, "")
            config.set(self._LAYOUT_KEY_STATE, "")
            config.set(self._LAYOUT_KEY_SCHEMA, 0)
        except Exception:  # noqa: BLE001
            log.exception("Failed to clear persisted layout")

    def _save_layout(self) -> None:
        try:
            geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
            state = bytes(self.saveState(self._LAYOUT_SCHEMA_VERSION).toBase64()).decode("ascii")
            config.set(self._LAYOUT_KEY_GEOMETRY, geometry)
            config.set(self._LAYOUT_KEY_STATE, state)
            config.set(self._LAYOUT_KEY_SCHEMA, self._LAYOUT_SCHEMA_VERSION)
        except Exception:  # noqa: BLE001
            log.exception("Failed to save dock layout")

    def _restore_layout(self) -> bool:
        """Restore geometry + dock state from QSettings.

        Returns True if a saved layout was applied, False on first launch /
        schema mismatch / corrupt blob (caller can fall back to defaults).
        """
        try:
            schema = int(config.get(self._LAYOUT_KEY_SCHEMA) or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema != self._LAYOUT_SCHEMA_VERSION:
            return False

        geometry_b64 = config.get(self._LAYOUT_KEY_GEOMETRY) or ""
        state_b64 = config.get(self._LAYOUT_KEY_STATE) or ""
        if not geometry_b64 or not state_b64:
            return False

        try:
            geometry = QByteArray.fromBase64(geometry_b64.encode("ascii"))
            state = QByteArray.fromBase64(state_b64.encode("ascii"))
        except Exception:  # noqa: BLE001
            log.exception("Stored layout blobs are corrupt; falling back to defaults")
            return False

        if not self.restoreGeometry(geometry):
            log.warning("restoreGeometry rejected the saved blob; using defaults")
            return False
        if not self.restoreState(state, self._LAYOUT_SCHEMA_VERSION):
            log.warning("restoreState rejected the saved blob; using defaults")
            return False
        return True

    def _apply_default_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1600, 950)
            return
        avail = screen.availableGeometry()
        w = min(1600, avail.width())
        h = min(950, avail.height())
        self.resize(w, h)
        x = avail.x() + (avail.width() - w) // 2
        y = avail.y() + (avail.height() - h) // 2
        self.move(x, y)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.settings_changed.connect(self._on_settings_changed)
        dlg.exec()

    def _on_settings_changed(self):
        reset_sdk()
        # Re-evaluate Blender availability — the user may have just set the
        # path or pointed it at a missing binary.
        from core.blender_runner import reset_blender_validation_cache
        reset_blender_validation_cache()
        self._refresh_blender_dependent_ui()
        self._log.append("Settings updated", "info")

    def _refresh_blender_dependent_ui(self):
        """Enable/disable Blender-touching UI based on ``is_blender_available()``.

        Called once on startup and whenever settings change. Keeps the Blend
        Combiner tab visible-but-greyed when Blender is missing so the user
        sees the feature exists; the Process Queue button is hard-disabled
        and the status bar carries a persistent reminder.
        """
        from core.blender_runner import is_blender_available
        available = is_blender_available()

        # Only toggle enabled state — renaming the dock title would also
        # rename the Window-menu entry (toggleViewAction tracks the title),
        # which we want kept stable. The status bar carries the "no Blender"
        # warning instead.
        self._combiner_dock.setEnabled(available)

        if hasattr(self, "_blend_combiner_action") and self._blend_combiner_action is not None:
            self._blend_combiner_action.setEnabled(available)

        if available:
            tip = ""
        else:
            tip = "Blender not configured — set blender_exe in Settings."
        self._queue.set_processing_enabled(available, tooltip=tip)

        if not available:
            self._statusbar.showMessage(
                "Blender not found — processing & Blend Combiner disabled. "
                "Set the path in Settings."
            )

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _start_scan(self):
        game_folder = config.get("game_folder")
        if not game_folder:
            QMessageBox.warning(
                self, "No Game Folder",
                "Set the game folder under Manage Profiles first."
            )
            return

        dll_path = config.get("everything_dll") or None

        try:
            sdk = get_sdk(dll_path)
        except EverythingError as e:
            QMessageBox.critical(
                self, "Everything SDK Error",
                f"Could not initialize Everything SDK:\n{e}\n\n"
                "Make sure Everything is running and the DLL path is correct."
            )
            return

        presets = config.load_presets()
        scanner = AssetScanner(
            game_folder, presets, sdk, **self._texture_resolution_kwargs()
        )

        # Seed scanner with any already-loaded cached entries so it skips them
        current_assets = self._browser.get_assets()
        if current_assets:
            scanner.seed_cache(current_assets)

        self._browser.set_scan_running(True)
        self._statusbar.showMessage("Scanning...")
        self._log.append(f"Scanning: {game_folder}", "info")

        self._scan_worker = ScanWorker(scanner, self)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._track_worker(self._scan_worker)
        self._scan_worker.start()

    @Slot(int, int, str)
    def _on_scan_progress(self, current: int, total: int, message: str):
        self._statusbar.showMessage(f"Scanning: {message} ({current}/{total})")
        self._queue.update_resolve_progress(current, total, message)

    def _cancel_scan(self):
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            # Keep Scan disabled until the worker actually finishes; only
            # gray out Cancel itself so a double-click can't re-fire it.
            self._browser._cancel_scan_btn.setEnabled(False)  # cancel-in-flight indicator
            self._log.append("Cancelling scan...", "warning")
            self._statusbar.showMessage("Cancelling scan...")

    @Slot(list)
    def _on_scan_finished(self, assets: list):
        self._browser.set_scan_running(False)
        self._browser.set_assets(assets)
        ready = sum(1 for a in assets if a.status == "ready")
        count_msg = f"{len(assets)} assets found, {ready} ready"
        self._log.append(f"Scan finished: {count_msg}", "success")
        self._statusbar.showMessage(f"Scan finished: {count_msg}")
        # Seed picker with already-processed paths
        processed = [a.psk_path for a in assets if a.processed]
        if processed:
            self._psk_picker.mark_processed(processed)

        # Save cache asynchronously
        game_folder = config.get("game_folder")
        if game_folder and assets:
            self._save_cache_async(
                assets, game_folder,
                on_done_msg=f"Scan cached ({len(assets)} assets) — will auto-load next time",
            )

    @Slot(str)
    def _on_scan_error(self, error: str):
        self._browser.set_scan_running(False)
        self._log.append(f"Scan failed: {error}", "error")
        self._statusbar.showMessage("Scan failed")
        QMessageBox.critical(self, "Scan Error", error)

    # ------------------------------------------------------------------
    # Profile system
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Profiles menu (replaces the old ProfileBar widget)
    # ------------------------------------------------------------------

    def _rebuild_profiles_menu(self) -> None:
        """Repopulate the Profiles menu from disk.

        Called on startup, on aboutToShow, and after any profile create /
        rename / delete. The active profile gets a checkmark via the
        exclusive action group.
        """
        from PySide6.QtGui import QAction

        if not hasattr(self, "_profiles_menu"):
            return

        # Tear down the previous action group + menu items.
        for action in list(self._profile_action_group.actions()):
            self._profile_action_group.removeAction(action)
        self._profiles_menu.clear()

        names = self._profile_manager.list_profiles()
        for name in names:
            action = QAction(name, self._profiles_menu)
            action.setCheckable(True)
            action.setChecked(name == self._active_profile_name)
            action.triggered.connect(lambda _checked=False, n=name: self._on_profile_menu_selected(n))
            self._profile_action_group.addAction(action)
            self._profiles_menu.addAction(action)

        if names:
            self._profiles_menu.addSeparator()

        manage_action = QAction("&Manage Profiles...", self._profiles_menu)
        manage_action.triggered.connect(self._open_profile_dialog)
        self._profiles_menu.addAction(manage_action)

    def _on_profile_menu_selected(self, name: str) -> None:
        """User clicked a profile entry in the Profiles menu."""
        if self._suppress_profile_menu_event:
            return
        if not name or name == self._active_profile_name:
            return

        # Busy check — same UX the old ProfileBar combo had.
        if self._is_busy():
            reply = QMessageBox.question(
                self,
                "Active Operation",
                "An operation is currently running.\n"
                "Cancel it and switch profiles?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # Revert the menu's check-state to the still-active profile.
                self._refresh_profile_menu_check()
                return
            self._cancel_active_ops()

        self._switch_profile(name)

    def _refresh_profile_menu_check(self) -> None:
        """Sync menu check-state with self._active_profile_name without
        triggering the action's `triggered` signal (we use this to revert
        a cancelled busy-check switch)."""
        if not hasattr(self, "_profile_action_group"):
            return
        self._suppress_profile_menu_event = True
        try:
            for action in self._profile_action_group.actions():
                action.setChecked(action.text() == self._active_profile_name)
        finally:
            self._suppress_profile_menu_event = False

    def _set_active_profile(self, name: str) -> None:
        """Update the active-profile state + menu checkmark in one call."""
        self._active_profile_name = name
        self._refresh_profile_menu_check()

    def _load_initial_profile(self):
        """On startup, load the last-used profile (or first available)."""
        profiles = self._profile_manager.list_profiles()
        if not profiles:
            self._profile_manager.create_profile("Default")
            profiles = ["Default"]

        saved = config.get("active_profile") or ""
        if saved in profiles:
            target = saved
        else:
            target = profiles[0]

        self._migrate_legacy_aes_keys(target)
        self._set_active_profile(target)
        self._rebuild_profiles_menu()
        self._load_profile(target)

    def _migrate_legacy_aes_keys(self, profile_name: str) -> None:
        """One-shot: copy legacy global ``aes_keys`` into the active profile.

        The Unpacker used to write a duplicate copy of the AES keys to global
        QSettings via ``config.set('aes_keys', ...)``. Now that the Unpacker
        no longer has its own AES editor, the profile JSON is the single
        source of truth. To avoid losing keys for users upgrading past this
        change, copy the legacy global blob into the profile *once* if the
        profile's own ``aes_keys`` field is empty.
        """
        legacy = config.get("aes_keys")
        if not legacy:
            return
        try:
            data = self._profile_manager.load_profile(profile_name)
        except Exception:  # noqa: BLE001
            return
        if data.get("aes_keys"):
            # Profile already has keys — drop the legacy blob to avoid
            # re-running this on every launch.
            try:
                config.set("aes_keys", "")
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            import json as _json
            keys = _json.loads(legacy)
        except Exception:  # noqa: BLE001
            try:
                config.set("aes_keys", "")
            except Exception:  # noqa: BLE001
                pass
            return
        if not isinstance(keys, list) or not keys:
            try:
                config.set("aes_keys", "")
            except Exception:  # noqa: BLE001
                pass
            return
        data["aes_keys"] = keys
        try:
            self._profile_manager.save_profile(profile_name, data)
            config.set("aes_keys", "")
            log.info("Migrated %d legacy AES key(s) into profile %s", len(keys), profile_name)
        except Exception:  # noqa: BLE001
            log.exception("Failed to migrate legacy AES keys into profile %s", profile_name)

    @Slot(int, list)
    def _on_aes_keys_required(self, unmounted_count: int, unmounted_archives: list) -> None:
        """Show the AES prompt and remount on accept."""
        from gui.aes_prompt_dialog import AesPromptDialog

        if not self._current_profile_name:
            return

        try:
            data = self._profile_manager.load_profile(self._current_profile_name)
        except Exception:  # noqa: BLE001
            log.exception("Failed to load profile for AES prompt")
            return

        existing_keys = list(data.get("aes_keys") or [])
        dlg = AesPromptDialog(
            unmounted_count=unmounted_count,
            archive_names=list(unmounted_archives or []),
            existing_keys=existing_keys,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._log.append(
                f"AES prompt cancelled — {unmounted_count} archive(s) remain unmounted",
                "warning",
            )
            return

        new_keys = dlg.result_keys()
        data["aes_keys"] = new_keys
        try:
            self._profile_manager.save_profile(self._current_profile_name, data)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Profile save failed", str(e))
            return

        # Push fresh keys into the Unpacker's snapshot and reissue mount.
        self._unpacker_panel.apply_profile_aes_keys(new_keys)
        self._log.append(
            f"Saved {len(new_keys)} AES key(s) to profile {self._current_profile_name}; remounting...",
            "info",
        )
        QTimer.singleShot(0, self._unpacker_panel._mount_archives)

    def _maybe_prompt_resume(self):
        """If a queue checkpoint exists for the current profile, ask to resume.

        Mismatched profiles leave the checkpoint untouched — the user can
        switch to that profile and the prompt will fire next launch. A
        broken / older-version checkpoint is silently dropped via
        ``queue_checkpoint.load`` returning None (the file stays so the
        user can investigate manually).
        """
        from core import queue_checkpoint
        if not queue_checkpoint.exists():
            return
        payload = queue_checkpoint.load()
        if payload is None:
            return
        if payload.profile and payload.profile != self._current_profile_name:
            log.info(
                "Skipping resume prompt: checkpoint is for profile %r, current is %r",
                payload.profile, self._current_profile_name,
            )
            return

        remaining = payload.remaining
        if not remaining:
            # Nothing left to do — checkpoint is stale, drop it.
            queue_checkpoint.delete()
            return

        reply = QMessageBox.question(
            self, "Resume previous batch?",
            f"A previous batch was interrupted with {len(remaining)} asset(s) "
            f"still pending (profile: {payload.profile or 'unknown'}).\n\n"
            "Resume processing now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            queue_checkpoint.delete()
            return

        # Add the remaining assets to the queue and kick off processing.
        # The JobManager will be constructed with already_completed so its
        # checkpoint writes continue from where we left off.
        self._queue.add_to_queue(remaining)
        self._resume_completed = list(payload.completed)
        self._log.append(
            f"Resuming previous batch ({len(remaining)} pending)", "info"
        )
        self._process_queue()

    def _running_workers(self) -> list[QThread]:
        """Return active workers whose underlying Qt object is still alive.

        ``QThread.isRunning`` raises ``RuntimeError`` when the C++ side has
        been deleted but the Python wrapper is still pinned in
        ``_active_workers`` (e.g. ``deleteLater`` fired before the
        ``finished`` slot pruned the entry — possible if the worker's parent
        chain processed a deferred-delete first). We treat any such entry as
        "not running" and prune it from the list so callers don't trip over
        it twice.
        """
        live: list[QThread] = []
        dead_idx: list[int] = []
        for idx, w in enumerate(self._active_workers):
            try:
                if w.isRunning():
                    live.append(w)
            except RuntimeError:
                dead_idx.append(idx)
        for idx in reversed(dead_idx):
            del self._active_workers[idx]
        return live

    def _is_busy(self) -> bool:
        """Return True if any background operation is running."""
        if self._running_workers():
            return True
        if self._unpacker_panel.is_exporting:
            return True
        if self._job_manager and self._job_manager.isRunning():
            return True
        if self._pending_cache_writes > 0:
            return True
        return False

    def _cancel_active_ops(self):
        """Cancel whatever background operation is running."""
        for worker in list(self._active_workers):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    log.exception("Worker cancel raised: %s", type(worker).__name__)
        if self._unpacker_panel.is_exporting:
            self._unpacker_panel.cancel_export()
        if self._job_manager and self._job_manager.isRunning():
            self._cancel_processing()

    def _texture_resolution_kwargs(self) -> dict:
        """Resolve the active profile's texture-resolution settings.

        Returns kwargs to pass to ``AssetScanner(...)``: profile_overrides,
        profile_preset, and fallback_enabled. Falls back to in-memory
        defaults when no profile is loaded or the profile JSON can't be
        read — the scanner's own defaults are equivalent so this never
        breaks scan flow.
        """
        name = self._current_profile_name
        if not name:
            return {}
        try:
            data = self._profile_manager.load_profile(name)
        except (FileNotFoundError, ProfileLoadError):
            return {}
        return {
            "profile_overrides": data.get("material_overrides") or {},
            "profile_preset": data.get("texture_preset") or "default_pbr",
            "fallback_enabled": bool(data.get("auto_resolve_fallback", True)),
        }

    def _save_current_profile(self):
        """Persist the current UI state into the active profile JSON.

        Path fields from the Unpacker tab (game_dir / unpack_output_dir /
        ue_version / mappings_path / aes_keys) are written back only when the
        profile has ``auto_save_paths`` enabled. Otherwise the on-disk profile
        is the source of truth and the Unpacker tab is purely a session-local
        editor — see the Manage Profiles dialog to mutate path fields.
        """
        name = self._current_profile_name
        if not name:
            return

        try:
            data = self._profile_manager.load_profile(name)
        except FileNotFoundError:
            data = {}
        except ProfileLoadError as e:
            # Refuse to silently overwrite a profile we couldn't read — that
            # would clobber the user's data with whatever the panels have in
            # memory. Surface the error and bail.
            log.error("Cannot save profile '%s': %s", name, e)
            self._log.append(f"Cannot save profile '{name}': {e}", "error")
            return

        # Always merge picker state (psk_processed) — that's a record of work
        # done, not a path the user is editing.
        data.update(self._psk_picker.collect_for_profile())

        # Path fields: only auto-merge when explicitly opted-in per profile.
        if data.get("auto_save_paths", False):
            data.update(self._unpacker_panel.collect_for_profile())

        # blender_output_dir falls back to the legacy global key for users
        # who haven't yet set it via Manage Profiles.
        data["blender_output_dir"] = data.get("blender_output_dir", "") or config.get("output_dir")

        # Scan cache file is keyed by the saved game_dir, not whatever the
        # Unpacker tab has typed in right now.
        game_dir = data.get("game_dir", "")
        if game_dir:
            import hashlib
            folder_hash = hashlib.md5(game_dir.encode()).hexdigest()[:12]
            data["scan_cache_file"] = f"scan_{folder_hash}.json"

        # Persist colour scheme into the profile
        data["color_scheme"] = config.get("color_scheme") or theme.current_scheme_name()

        self._profile_manager.save_profile(name, data)
        log.debug("Saved profile: %s", name)

    def _open_profile_dialog(self):
        """Open the Manage Profiles dialog and react to its outcomes."""
        from gui.profile_dialog import ProfileDialog

        # Save in-flight state first so it isn't lost if the user renames /
        # deletes the active profile.
        self._save_current_profile()

        dlg = ProfileDialog(
            self._profile_manager,
            current_profile=self._current_profile_name,
            parent=self,
        )

        # Track outcomes so we can surface the right log line + reload state.
        renames: list[tuple[str, str]] = []
        creates: list[str] = []
        deletes: list[str] = []
        dlg.profile_renamed.connect(lambda old, new: renames.append((old, new)))
        dlg.profile_created.connect(lambda n: creates.append(n))
        dlg.profile_deleted.connect(lambda n: deletes.append(n))

        result = dlg.exec()

        # Always refresh the dropdown — names may have changed even if the
        # user clicked Cancel (New / Rename / Delete commit immediately).
        active = self._current_profile_name
        for old, new in renames:
            if active == old:
                active = new
                config.set("active_profile", new)
                self.setWindowTitle(f"EfficientAssetRipper v{__version__} — {new}")
            self._log.append(f"Renamed profile: {old} → {new}", "info")
        for n in creates:
            self._log.append(f"Created profile: {n}", "info")
        for n in deletes:
            self._log.append(f"Deleted profile: {n}", "info")
            if active == n:
                # Active profile was deleted — fall back to the first remaining
                remaining = self._profile_manager.list_profiles()
                active = remaining[0] if remaining else ""

        # If OK was pressed, on-disk path values may have changed for the
        # active profile (e.g. user edited Mounted Folder). Reload state so
        # the Unpacker panel and config bridges reflect the new values.
        needs_reload = (
            result == 1  # QDialog.Accepted
            or active != self._current_profile_name
        )
        if active and needs_reload:
            self._load_profile(active)

        self._set_active_profile(active or "")
        self._rebuild_profiles_menu()

    def _load_profile(self, name: str):
        """Load a profile from disk and push its state to all panels."""
        try:
            data = self._profile_manager.load_profile(name)
        except FileNotFoundError:
            self._log.append(f"Profile '{name}' not found", "warning")
            return
        except ProfileLoadError as e:
            log.error("Failed to load profile '%s': %s", name, e)
            QMessageBox.critical(
                self, "Profile load failed",
                f"Could not read profile '{name}'.\n\n{e}\n\n"
                "Close any program that may be holding the file open, "
                "or open the profiles folder to inspect it manually.",
            )
            self._log.append(f"Failed to load profile '{name}': {e}", "error")
            return

        self._current_profile_name = name
        config.set("active_profile", name)
        self._set_active_profile(name)

        # Push state to panels
        self._unpacker_panel.load_from_profile(data)

        # Clear queue and log when switching profiles
        self._queue.clear_queue()
        self._log.clear()

        # Also set game_folder in global config so other parts (scanner, etc.) can read it
        game_dir = data.get("game_dir", "")
        config.set("game_folder", game_dir)
        config.set("unpack_output_dir", data.get("unpack_output_dir", ""))
        config.set("unpack_ue_version", data.get("ue_version", "GAME_UE5_4"))

        # Apply profile's colour scheme if it has one
        profile_scheme = data.get("color_scheme", "")
        if profile_scheme:
            config.set("color_scheme", profile_scheme)
            app = QApplication.instance()
            if app:
                theme.apply(app, profile_scheme)

        blender_out = data.get("blender_output_dir", "")
        if blender_out:
            config.set("output_dir", blender_out)

        # Load scan cache for this profile
        self._browser.set_assets([])  # clear first
        scan_cache_file = data.get("scan_cache_file", "")
        if game_dir and scan_cache_file:
            result = load_scan_cache(game_dir)
            if result is not None:
                assets, timestamp = result
                import datetime
                age = datetime.datetime.now() - datetime.datetime.fromtimestamp(timestamp)
                if age.days > 0:
                    age_str = f"{age.days}d ago"
                elif age.seconds >= 3600:
                    age_str = f"{age.seconds // 3600}h ago"
                else:
                    age_str = f"{age.seconds // 60}m ago"

                self._browser.set_assets(assets)
                ready = sum(1 for a in assets if a.status == "ready")
                msg = f"[{name}] Loaded {len(assets)} cached assets ({ready} ready, scanned {age_str})"
                self._log.append(msg, "info")
                self._statusbar.showMessage(msg)

                # Seed picker with already-processed paths
                processed = [a.psk_path for a in assets if a.processed]
                if processed:
                    self._psk_picker.mark_processed(processed)

        # Load PSK picker state (after cache so processed lists merge correctly)
        self._psk_picker.load_from_profile(data)

        self.setWindowTitle(f"EfficientAssetRipper v{__version__} — {name}")

    def _switch_profile(self, name: str):
        """Save current profile, then load the new one."""
        if name == self._current_profile_name:
            return
        self._save_current_profile()
        self._load_profile(name)
        self._set_active_profile(name)

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _add_browser_to_queue(self, assets: list[AssetEntry]):
        """Add assets from the browser's 'Add to Queue' button."""
        count = len(assets)
        self._queue.add_to_queue(assets)
        self._log.append(f"Added {count} assets to queue from browser", "info")
        # Switch to queue tab so user sees the result
        self._raise_dock(self._queue_dock)

    def _add_picker_to_queue(self, paths: list[Path]):
        """Resolve PSK paths from the picker and add to queue."""
        # Use unpack_output_dir (where exported PSK/props/TGA files live) when
        # set; fall back to game_folder for single-directory setups.
        game_folder = config.get("unpack_output_dir") or config.get("game_folder")
        if not game_folder:
            QMessageBox.warning(self, "No Game Folder", "Set the game folder under Manage Profiles first.")
            return

        dll_path = config.get("everything_dll") or None
        try:
            sdk = get_sdk(dll_path)
        except EverythingError as e:
            QMessageBox.critical(self, "Everything SDK Error", str(e))
            return

        presets = config.load_presets()
        scanner = AssetScanner(
            game_folder, presets, sdk, **self._texture_resolution_kwargs()
        )

        self._log.append(f"Resolving {len(paths)} picked PSK files...", "info")
        self._statusbar.showMessage(f"Resolving {len(paths)} picked files...")

        # Build stub AssetEntry objects for each path
        from core.classifier import classify
        entries: list[AssetEntry] = []
        for p in paths:
            e = AssetEntry(psk_path=p, name=p.stem)
            cat = classify(p, game_folder)
            e.category = cat.category
            e.subcategory = cat.subcategory
            entries.append(e)

        self._picker_entries = entries
        self._picker_resolved_entries: list[AssetEntry] = []
        self._picker_worker = _PickerResolveWorker(scanner, entries, self)
        self._picker_worker.progress.connect(self._on_scan_progress)
        self._picker_worker.entry_resolved.connect(self._on_picker_entry_resolved)
        self._picker_worker.finished.connect(self._on_picker_resolved)
        self._picker_worker.error.connect(self._on_scan_error)
        self._track_worker(self._picker_worker)
        self._queue.set_resolving(True)
        self._raise_dock(self._queue_dock)
        self._picker_worker.start()

    @Slot(object)
    def _on_picker_entry_resolved(self, entry: AssetEntry):
        """Add each resolved entry to the queue immediately as it finishes."""
        self._picker_resolved_entries.append(entry)
        self._queue.add_to_queue([entry])

    @Slot(int, int)
    def _on_picker_resolved(self, resolved: int, still_incomplete: int):
        self._queue.set_resolving(False)
        entries = self._picker_resolved_entries
        self._merge_entries_into_browser(entries)
        msg = f"Added {len(entries)} picked assets to queue ({resolved} ready)"
        self._log.append(msg, "info")
        self._statusbar.showMessage(msg)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process_queue(self):
        """Process all pending items currently in the queue."""
        pending = self._queue.get_pending_assets()
        if pending:
            self._start_processing(pending)
        else:
            QMessageBox.information(
                self, "Nothing to Process",
                "No pending items in the queue. Add assets first."
            )

    def _start_processing(self, assets: list[AssetEntry]):
        if not assets:
            QMessageBox.information(
                self, "Nothing Selected",
                "Select assets in the browser first."
            )
            return

        blender_exe = config.get("blender_exe")
        if not blender_exe:
            QMessageBox.warning(
                self, "Blender Not Set",
                "Set the Blender executable path in Settings.\n\n"
                "Until then, Process Queue and the Blend Combiner tab are disabled."
            )
            return

        output_dir = config.get("output_dir")
        if not output_dir:
            QMessageBox.warning(
                self, "Output Directory Not Set",
                "Set the output directory under Manage Profiles."
            )
            return

        addon_name = config.get("psk_addon_name")
        timeout = config.get_int("timeout_seconds") or 120

        # Set up queue panel for this batch
        batch_offset = self._queue.get_pending_offset()
        self._queue.begin_processing(batch_offset, len(assets))
        self._queue.set_processing(True)

        self._log.append(
            f"Starting batch: {len(assets)} assets → {output_dir}", "info"
        )

        self._job_manager = JobManager(
            assets=assets,
            blender_exe=blender_exe,
            output_dir=output_dir,
            addon_name=addon_name,
            timeout=timeout,
            parent=self,
            profile_name=self._current_profile_name,
            already_completed=getattr(self, "_resume_completed", None),
        )
        # The completed-paths carry-over only applies to the very next batch;
        # subsequent runs start fresh.
        self._resume_completed = None

        # Connect signals
        self._job_manager.job_started.connect(self._queue.on_job_started)
        self._job_manager.job_completed.connect(self._queue.on_job_completed)
        self._job_manager.job_progress.connect(self._queue.on_job_progress)
        self._job_manager.queue_finished.connect(self._queue.on_queue_finished)
        self._job_manager.log_message.connect(self._log.append)
        self._job_manager.queue_finished.connect(self._on_processing_done)

        self._job_manager.start()

    def _cancel_processing(self):
        if self._job_manager and self._job_manager.isRunning():
            self._job_manager.cancel()
            self._log.append("Cancelling batch...", "warning")
        picker_worker = getattr(self, "_picker_worker", None)
        if picker_worker and picker_worker.isRunning():
            picker_worker.cancel()
            self._log.append("Cancelling asset resolve...", "warning")
            self._statusbar.showMessage("Cancelling resolve...")

    @Slot(int, int, int)
    def _on_processing_done(self, total: int, succeeded: int, failed: int):
        self._statusbar.showMessage(
            f"Batch done: {succeeded}/{total} succeeded, {failed} failed"
        )
        # Refresh browser to show blend paths and processed status
        self._browser.refresh_tree()
        # Re-save cache with updated blend_path / processed data
        self._save_cache()
        # Mark processed items in PSK picker so they can't be re-queued
        processed = [
            a.psk_path for a in self._browser.assets if a.processed
        ]
        if processed:
            self._psk_picker.mark_processed(processed)

    # ------------------------------------------------------------------
    # Reprocess
    # ------------------------------------------------------------------

    def _reprocess_asset(self, asset):
        """Reprocess a single asset (overwrites existing .blend)."""
        asset.processed = False
        self._queue.add_to_queue([asset])
        self._raise_dock(self._queue_dock)
        self._log.append(f"Queued for reprocessing: {asset.name}", "info")

    # ------------------------------------------------------------------
    # Browser / cache helpers
    # ------------------------------------------------------------------

    def _merge_entries_into_browser(self, entries: list):
        """Merge entries into the browser's asset list (de-duplicate by psk_path)."""
        existing = {str(a.psk_path): a for a in self._browser._assets}
        added = 0
        for e in entries:
            key = str(e.psk_path)
            if key not in existing:
                self._browser._assets.append(e)
                existing[key] = e
                added += 1
            else:
                # Update the existing entry with any new data
                ex = existing[key]
                if not ex.materials and e.materials:
                    ex.materials = e.materials
                    ex.total_textures = e.total_textures
                    ex.missing_textures = e.missing_textures
                    ex.mesh_props_found = e.mesh_props_found
        if added:
            self._browser._populate_category_filter()
        self._browser.refresh_tree()

    def _save_cache(self):
        """Save the browser's current assets to the scan cache and update profile."""
        game_folder = config.get("game_folder")
        assets = self._browser.assets
        if game_folder and assets:
            self._save_cache_async(assets, game_folder)
        self._save_current_profile()

    def _save_cache_async(
        self,
        assets: list,
        game_folder: str,
        on_done_msg: str | None = None,
    ) -> None:
        """Submit a cache write to the global QThreadPool. Tracks pending count."""
        self._pending_cache_writes += 1
        self._statusbar.showMessage("Saving cache...", 1500)

        def _done():
            # _on_cache_write_done runs in the worker thread; bounce to GUI
            # thread via QTimer.singleShot so signal-slot invariants hold.
            QTimer.singleShot(0, lambda: self._on_cache_write_done(on_done_msg))

        runnable = _CacheWriteRunnable(assets, game_folder, on_done=_done)
        QThreadPool.globalInstance().start(runnable)

    def _on_cache_write_done(self, msg: str | None):
        if self._pending_cache_writes > 0:
            self._pending_cache_writes -= 1
        if msg:
            self._log.append(msg, "info")

    def _on_browser_delete(self, assets: list):
        """Handle deletion of assets from the browser — save updated cache."""
        names = [a.name for a in assets]
        self._log.append(f"Removed {len(assets)} asset(s): {', '.join(names)}", "info")
        # Un-mark deleted assets in the picker so they become selectable again
        self._psk_picker.unmark_processed([a.psk_path for a in assets])
        self._save_cache()

    # ------------------------------------------------------------------
    # Re-scan incomplete entries
    # ------------------------------------------------------------------

    def _rescan_selected(self, entries: list):
        game_folder = config.get("game_folder")
        if not game_folder:
            QMessageBox.warning(self, "No Game Folder", "Set the game folder under Manage Profiles first.")
            return

        dll_path = config.get("everything_dll") or None
        try:
            sdk = get_sdk(dll_path)
        except EverythingError as e:
            QMessageBox.critical(self, "Everything SDK Error", str(e))
            return

        presets = config.load_presets()
        scanner = AssetScanner(
            game_folder, presets, sdk, **self._texture_resolution_kwargs()
        )

        self._log.append(f"Re-scanning {len(entries)} incomplete entries...", "info")
        self._browser.set_scan_running(True)
        self._statusbar.showMessage(f"Re-scanning {len(entries)} entries...")

        self._rescan_worker = RescanWorker(scanner, entries, self)
        self._rescan_worker.progress.connect(self._on_scan_progress)
        self._rescan_worker.finished.connect(self._on_rescan_finished)
        self._rescan_worker.error.connect(self._on_scan_error)
        self._track_worker(self._rescan_worker)
        self._rescan_worker.start()

    @Slot(int, int)
    def _on_rescan_finished(self, resolved: int, still_incomplete: int):
        self._browser.set_scan_running(False)
        self._browser.refresh_tree()
        msg = f"Re-scan done: {resolved} resolved, {still_incomplete} still incomplete"
        self._log.append(msg, "success" if still_incomplete == 0 else "info")
        self._statusbar.showMessage(msg)

        # Update cache
        game_folder = config.get("game_folder")
        assets = self._browser.assets
        if game_folder and assets:
            self._save_cache_async(
                assets, game_folder, on_done_msg="Cache updated"
            )

    # ------------------------------------------------------------------
    # Unpacker hand-off
    # ------------------------------------------------------------------

    def _on_psks_extracted(self, psk_paths: list):
        """Receive extracted PSK files from the Unpacker and add to queue.

        Routed through ``_add_picker_to_queue`` so each PSK gets fully
        resolved (mesh props → material refs → texture lookup) before it
        lands in the queue. Skipping the resolve step here previously
        produced phantom entries with empty ``materials`` — they would
        process but every Blender material warned ``No texture spec for
        material: X (available: [])`` because ``to_manifest`` saw nothing
        to wire.
        """
        if not psk_paths:
            return
        self._add_picker_to_queue(psk_paths)

    def closeEvent(self, event):
        """Save profile and stop the CLI process on exit."""
        # Save the in-memory profile state up front. If we wait until *after*
        # cancellation, a partially-cancelled scan could lose its picker /
        # unpacker state on the way out.
        try:
            self._save_current_profile()
        except Exception:
            log.exception("save_current_profile during shutdown raised")

        # Persist dock layout + window geometry so the next launch reopens
        # in the same arrangement.
        self._save_layout()

        running_workers = self._running_workers()
        job_running = bool(self._job_manager and self._job_manager.isRunning())
        export_running = bool(self._unpacker_panel.is_exporting)

        if running_workers or job_running or export_running:
            reply = QMessageBox.question(
                self, "Cancel running operations?",
                "Background work is still running. Cancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self._cancel_active_ops()

        # Wait for workers to drain. We give each worker a reasonable budget
        # before falling back to terminate(); QProgressDialog keeps the user
        # informed instead of hanging silently.
        progress = None
        if running_workers or job_running:
            progress = QProgressDialog("Stopping background work...", None, 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.ApplicationModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(0)
            progress.show()
            QApplication.processEvents()

        for worker in running_workers:
            try:
                if not worker.wait(5000):
                    log.warning(
                        "Worker %s did not exit cleanly within 5s — terminating",
                        type(worker).__name__,
                    )
                    worker.terminate()
                    worker.wait(2000)
            except Exception:
                log.exception("Error waiting for worker %s", type(worker).__name__)

        if self._job_manager and self._job_manager.isRunning():
            try:
                if not self._job_manager.wait(5000):
                    log.warning("JobManager did not exit cleanly — terminating")
                    self._job_manager.terminate()
                    self._job_manager.wait(2000)
            except Exception:
                log.exception("Error waiting for JobManager")

        # Drain any in-flight cache writes (best-effort).
        try:
            QThreadPool.globalInstance().waitForDone(3000)
        except Exception:
            log.exception("waitForDone for cache writes raised")

        if progress is not None:
            progress.close()

        try:
            self._unpacker_panel.shutdown()
        except Exception:
            log.exception("UnpackerPanel.shutdown raised")
        # Block briefly on the update-checkers so a long-running HTTP timeout
        # can't keep their QThreads alive past the QObject lifetime.
        for checker in (self._update_checker, self._manual_update_checker):
            if checker is not None:
                try:
                    checker.shutdown(timeout_ms=2000)
                except Exception:  # noqa: BLE001
                    log.debug("update_checker.shutdown raised", exc_info=True)
        super().closeEvent(event)
