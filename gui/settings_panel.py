"""Settings dialog for configuring global paths, timeouts, presets, and appearance.

Per-profile paths (game folder, mounted folder, output folder) live in the
profile JSON and are edited via the Manage Profiles dialog — this dialog only
covers global tooling paths shared across every profile.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config
from gui.color_scheme_dialog import ColorSchemeDialog
from gui.color_schemes import list_scheme_names
from gui.widgets import CollapsibleSection, LabeledSlider, PathPicker
import gui.theme as theme


class SettingsDialog(QDialog):
    """Application settings dialog."""

    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(650)
        self.setMinimumHeight(500)

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        scroll.setWidget(scroll_widget)
        outer.addWidget(scroll, stretch=1)

        # Shared building blocks for section content
        border_hex = theme.current_scheme().get("border", "#444444")

        def _divider() -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFixedHeight(1)
            line.setStyleSheet(f"background-color: {border_hex}; border: none;")
            return line

        def _sub_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-weight: 600; font-size: {theme.FONT_SIZE_LARGE};")
            return lbl

        # --- Tooling paths (global; per-profile paths live in Manage Profiles) ---
        paths_form = QFormLayout()
        paths_label = _sub_label("Tooling Paths")
        paths_label.setToolTip(
            "These paths apply to every profile. Per-profile paths "
            "(Game folder / Mounted folder / Output folder) are edited under "
            "Manage Profiles."
        )

        self.blender_exe = PathPicker(
            mode="file",
            filter_str=(
                "Blender (blender.exe)"
                if sys.platform == "win32"
                else "Blender (blender);;All Files (*)"
            ),
            title="Select Blender Executable",
        )
        self.blender_exe.setText(
            config.get("blender_exe") or self._auto_detect_blender()
        )
        paths_form.addRow("Blender Executable:", self.blender_exe)

        everything_filter = (
            "DLL Files (*.dll);;All Files (*)" if sys.platform == "win32"
            else "All Files (*)"
        )
        self.everything_dll = PathPicker(
            mode="file", filter_str=everything_filter,
            title="Select Everything SDK DLL",
        )
        self.everything_dll.setText(
            config.get("everything_dll") or self._auto_detect_everything()
        )
        paths_form.addRow("Everything SDK DLL:", self.everything_dll)

        self.presets_path = PathPicker(
            mode="file", filter_str="JSON Files (*.json);;All Files (*)",
            title="Select Texture Presets JSON",
        )
        self.presets_path.setText(str(config.get_presets_path()))
        paths_form.addRow("Texture Presets JSON:", self.presets_path)

        cli_filter = (
            "Executable (CUE4ParseCLI.exe)" if sys.platform == "win32"
            else "Executable (CUE4ParseCLI);;All Files (*)"
        )
        self.cue4parse_cli = PathPicker(
            mode="file", filter_str=cli_filter,
            title="Select CUE4Parse CLI",
        )
        self.cue4parse_cli.setText(config.get_cue4parse_cli())
        paths_form.addRow("CUE4Parse CLI:", self.cue4parse_cli)

        # --- Processing group ---
        proc_form = QFormLayout()

        self.addon_name = QLineEdit(config.get("psk_addon_name"))
        proc_form.addRow("PSK Import Addon:", self.addon_name)

        self.timeout = LabeledSlider(10, 3600)
        self.timeout.setSuffix(" seconds")
        self.timeout.setValue(config.get_int("timeout_seconds") or 120)
        proc_form.addRow("Timeout per Asset:", self.timeout)

        # The top stop is this machine's logical thread count — the CLI can't
        # usefully queue more workers than cores, and a fixed 64-slot groove
        # on an 8-core box would just mislead. Floor >1 row keeps the slider
        # usable if detection fails (os.cpu_count() can return None).
        self.export_threads = LabeledSlider(0, max(1, os.cpu_count() or 64))
        self.export_threads.setSpecialValueText("Auto (all cores)")
        self.export_threads.setValue(config.get_int("unpacker_export_threads"))
        self.export_threads.setToolTip(
            "Parallel export workers inside the CUE4Parse CLI. "
            "Auto uses one worker per CPU core. Lower this if exports "
            "use too much memory or destabilize on this machine."
        )
        proc_form.addRow("Export Threads:", self.export_threads)

        self.gpu_accel = QComboBox()
        self.gpu_accel.addItems(["Auto", "On", "Off"])
        self.gpu_accel.setCurrentText(config.get("unpacker_gpu_accel").capitalize())
        self.gpu_accel.setToolTip(
            "Texture decode on the graphics card (Vulkan). Auto uses the GPU "
            "when one is available and falls back to CPU otherwise. Result "
            "is shown in the unpacker log at mount time."
        )
        proc_form.addRow("GPU Decode:", self.gpu_accel)

        # --- Export memory policy (enforced inside the CUE4Parse CLI) ---
        self.physical_memory_only = QCheckBox("Use only physical memory")
        stored_physical = config.get_raw("unpacker_physical_memory_only")
        self.physical_memory_only.setChecked(
            stored_physical in (True, "true", "True", 1))
        self.physical_memory_only.setToolTip(
            "Keep the exporter inside physical RAM — it never relies on "
            "swap/page file, pausing when the machine comes under memory "
            "pressure. Takes effect on the next Mount (the CLI is "
            "relaunched with a smaller-footprint garbage collector)."
        )
        proc_form.addRow("", self.physical_memory_only)

        self.memory_limit = LabeledSlider(0, 95)
        self.memory_limit.setSpecialValueText("Unlimited")
        self.memory_limit.setSuffix(" %")
        try:
            self.memory_limit.setValue(int(config.get("unpacker_memory_limit_percent") or 0))
        except (TypeError, ValueError):
            self.memory_limit.setValue(0)
        self.memory_limit.setToolTip(
            "Hard budget for an export run as a percentage of the RAM that "
            "is currently free — not total RAM — measured when each export "
            "starts (the exporter's own resident pages are not counted "
            "against you). Workers pause above the soft threshold and "
            "assets are skipped with a clear error if the limit can't be "
            "met. 0 = unlimited. Applies from the next export."
        )
        proc_form.addRow("Memory Limit:", self.memory_limit)

        self.memory_hint = QLabel()
        self.memory_hint.setWordWrap(True)
        proc_form.addRow("", self.memory_hint)
        self.memory_limit.valueChanged.connect(self._refresh_memory_hint)
        self.physical_memory_only.toggled.connect(self._refresh_memory_hint)
        self._refresh_memory_hint()

        # --- Export Formats group ---
        fmt_section = CollapsibleSection("Export Formats", start_expanded=True)
        fmt_form = QFormLayout()

        self.texture_format = QComboBox()
        self.texture_format.addItems(["png", "tga", "webp"])
        self.texture_format.setCurrentText(config.get("export_texture_format"))
        fmt_form.addRow("Textures:", self.texture_format)

        # WebP tuning — only meaningful when webp is the texture format; the
        # spin box stays populated so switching back preserves the value.
        self.webp_mode = QComboBox()
        self.webp_mode.addItems(["lossless", "lossy"])
        self.webp_mode.setCurrentText(str(config.get("export_texture_webp_mode")))
        self.webp_mode.currentTextChanged.connect(self._on_webp_mode_changed)
        self.webp_quality = LabeledSlider(1, 100)
        self.webp_quality.setValue(int(config.get("export_texture_webp_quality")))
        self.webp_quality.setSuffix(" %")
        self.webp_quality.setToolTip(
            "Lossy WebP quality. Ignored in lossless mode "
            "(lossless output is bit-exact at any setting)."
        )
        fmt_form.addRow("WebP mode:", self.webp_mode)
        fmt_form.addRow("WebP quality:", self.webp_quality)
        self.texture_format.currentTextChanged.connect(self._on_texture_format_changed)

        self.audio_format = QComboBox()
        self.audio_format.addItems(["wav", "ogg", "mp3", "webm"])
        self.audio_format.setCurrentText(config.get("export_audio_format"))
        fmt_form.addRow("Audio:", self.audio_format)

        self.mesh_format = QComboBox()
        self.mesh_format.addItems(["psk", "glb", "fbx"])
        self.mesh_format.setCurrentText(config.get("export_mesh_format") or "psk")
        fmt_form.addRow("Meshes:", self.mesh_format)
        mesh_hint = QLabel(
            "psk = PSK/PSKX (fastest)\n"
            "glb = glTF 2.0 binary, untextured\n"
            "fbx = exports PSK then converts through Blender (more meshes = slower)"
        )
        mesh_hint.setWordWrap(True)
        fmt_form.addRow("", mesh_hint)

        self._on_texture_format_changed(self.texture_format.currentText())

        # --- Presets shortcut (lives inside the Export Formats section,
        # right under the format pickers it belongs to) ---
        presets_btn = QPushButton("Open Texture Presets JSON in Editor")
        presets_btn.clicked.connect(self._open_presets)
        presets_row = QHBoxLayout()
        presets_row.addStretch()
        presets_row.addWidget(presets_btn)
        presets_row.addStretch()

        fmt_content = QVBoxLayout()
        fmt_content.addLayout(fmt_form)
        fmt_content.addLayout(presets_row)
        fmt_section.set_content_layout(fmt_content)
        layout.addWidget(fmt_section)

        # --- Appearance: theme picker + shortcut to the colour-scheme dialog ---
        appearance_section = CollapsibleSection("Appearance", start_expanded=True)
        appearance_grid = QGridLayout()
        appearance_grid.addWidget(QLabel("Theme:"), 0, 0)
        self.theme_combo = QComboBox()
        self._reload_theme_combo()
        self.theme_combo.currentTextChanged.connect(self._on_theme_selected)
        appearance_grid.addWidget(self.theme_combo, 0, 1)
        customize_btn = QPushButton("Customize Colors…")
        customize_btn.clicked.connect(self._open_color_scheme_dialog)
        appearance_grid.addWidget(customize_btn, 0, 2)
        appearance_grid.addWidget(_divider(), 1, 0, 1, 4)
        appearance_grid.addWidget(QLabel("UI Scale:"), 2, 0)
        self.ui_scale = LabeledSlider(theme.UI_SCALE_MIN, theme.UI_SCALE_MAX)
        self.ui_scale.setSingleStep(theme.UI_SCALE_STEP)
        self.ui_scale.setSuffix("%")
        self.ui_scale.setValue(round(theme.ui_scale() * 100))
        # Fill the same column as the theme combo so both controls share a
        # left edge and width; match the combo's height too.
        self.ui_scale.setMinimumHeight(self.theme_combo.sizeHint().height())
        appearance_grid.addWidget(self.ui_scale, 2, 1)
        appearance_grid.setColumnStretch(3, 1)
        appearance_section.set_content_layout(appearance_grid)
        layout.addWidget(appearance_section)

        # --- Test group ---
        test_btn = QPushButton("Test All Paths && SDKs")
        test_btn.setProperty("cssClass", "accent")
        test_btn.clicked.connect(self._run_tests)
        test_btn_row = QHBoxLayout()
        test_btn_row.addStretch()
        test_btn_row.addWidget(test_btn)
        test_btn_row.addStretch()

        self._test_output = QTextEdit()
        self._test_output.setReadOnly(True)

        # --- Combined tooling/setup block: last section, inline sub-groups
        # under one header, separated by divider lines ---
        setup_section = CollapsibleSection("Tooling & Setup", start_expanded=True)
        setup_layout = QVBoxLayout()
        setup_layout.addWidget(paths_label)
        setup_layout.addLayout(paths_form)
        setup_layout.addWidget(_divider())
        setup_layout.addWidget(_sub_label("Processing"))
        setup_layout.addLayout(proc_form)
        setup_layout.addWidget(_divider())
        setup_layout.addWidget(_sub_label("Verify Setup"))
        setup_layout.addLayout(test_btn_row)
        setup_layout.addWidget(self._test_output)
        setup_section.set_content_layout(setup_layout)

        layout.addWidget(setup_section)
        layout.addStretch()

        # --- Buttons (outside scroll area, always visible) ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self._reset_to_defaults
        )
        outer.addWidget(buttons)

        # Open tall enough to show every section expanded (capped to the
        # screen). Top-level dialogs never auto-shrink, so collapsing a
        # section afterwards leaves the window size alone.
        hint = scroll_widget.sizeHint()
        height = hint.height() + buttons.sizeHint().height() + 48
        width = max(self.minimumWidth(), hint.width() + 40)
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            height = min(height, avail.height() - 80)
            width = min(width, avail.width() - 80)
        self.resize(width, height)

    # Path-typed settings: keys checked for existence on save (file only —
    # folder fields all moved to per-profile JSON).
    _PATH_FIELDS_FILE = ("blender_exe", "everything_dll", "cue4parse_cli")

    # Mesh-format values stored in config (the combo shows the same tokens).
    _MESH_FORMATS = ("psk", "glb", "fbx")

    def _on_texture_format_changed(self, fmt: str):
        """Enable the WebP tuning widgets only when webp is selected."""
        is_webp = fmt.lower() == "webp"
        self.webp_mode.setEnabled(is_webp)
        self.webp_quality.setEnabled(is_webp and self.webp_mode.currentText() == "lossy")

    def _on_webp_mode_changed(self, mode: str):
        self.webp_quality.setEnabled(self.texture_format.currentText().lower() == "webp" and mode == "lossy")

    def _refresh_memory_hint(self):
        """Update the absolute-budget hint line under the memory widgets."""
        from core import memory_info
        label = memory_info.budget_label(
            self.memory_limit.value(),
            self.physical_memory_only.isChecked(),
        )
        self.memory_hint.setText(label or "")

    def _confirm_missing_path(self, label: str, value: str) -> bool:
        """Prompt the user when a path is set but doesn't exist on disk."""
        reply = QMessageBox.question(
            self,
            "Path doesn't exist",
            f"{label}:\n{value}\n\nThis path doesn't exist. Save anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _validate_paths(self) -> bool:
        """Return False if the user cancels at any non-existent path prompt."""
        checks: list[tuple[str, str, str]] = [  # (label, value, kind)
            ("Blender Executable", self.blender_exe.text().strip(), "file"),
            ("Everything SDK DLL", self.everything_dll.text().strip(), "file"),
            ("CUE4Parse CLI", self.cue4parse_cli.text().strip(), "file"),
        ]
        for label, value, kind in checks:
            if not value:
                continue
            ok = os.path.isdir(value) if kind == "folder" else os.path.isfile(value)
            if ok:
                continue
            if not self._confirm_missing_path(label, value):
                return False
        return True

    def _save(self):
        # Clamp the timeout into a sane range (the slider's setValue already
        # constrains via its range, but be defensive in case the constraint
        # changes).
        self.timeout.setValue(max(10, min(self.timeout.value(), 3600)))

        if not self._validate_paths():
            return

        config.set("blender_exe", self.blender_exe.text())
        config.set("everything_dll", self.everything_dll.text())
        # Presets path: a non-bundled location is allowed but requires a
        # one-time confirmation so a stray file substitution can't sneak in
        # during a settings round-trip.
        new_presets = self.presets_path.text().strip()
        if new_presets and not config.is_presets_path_safe(new_presets):
            already_confirmed = (
                config.get("presets_path_confirmed_external") == new_presets
            )
            if not already_confirmed:
                resp = QMessageBox.warning(
                    self,
                    "External texture_presets.json",
                    (
                        f"The selected texture_presets.json is outside the install dir:\n"
                        f"{new_presets}\n\n"
                        "Continue using this file? Click No to revert to the bundled defaults."
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    new_presets = str(config.base_dir() / "data" / "texture_presets.json")
                    self.presets_path.setText(new_presets)
                else:
                    config.set("presets_path_confirmed_external", new_presets)
        config.set("presets_path", new_presets)
        config.set("psk_addon_name", self.addon_name.text())
        config.set("timeout_seconds", self.timeout.value())
        config.set("unpacker_export_threads", self.export_threads.value())
        config.set("unpacker_physical_memory_only", self.physical_memory_only.isChecked())
        config.set("unpacker_memory_limit_percent", self.memory_limit.value())
        config.set("unpacker_gpu_accel", self.gpu_accel.currentText().lower())
        config.set("cue4parse_cli", self.cue4parse_cli.text())
        config.set("export_texture_format", self.texture_format.currentText())
        config.set("export_texture_webp_mode", self.webp_mode.currentText())
        config.set("export_texture_webp_quality", self.webp_quality.value())
        config.set("export_audio_format", self.audio_format.currentText())
        # Combo text doubles as the stored token ("psk"/"glb"/"fbx"); anything
        # else can only come from a corrupted QSettings load — keep psk.
        mesh_token = self.mesh_format.currentText().strip().lower()
        config.set("export_mesh_format", mesh_token if mesh_token in self._MESH_FORMATS else "psk")

        config.set("ui_scale", self.ui_scale.value())
        theme.set_ui_scale(self.ui_scale.value() / 100.0)
        app = QApplication.instance()
        if app:
            theme.apply(app)

        self.settings_changed.emit()
        self.accept()

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _reset_to_defaults(self):
        """Repopulate the dialog widgets from ``config._DEFAULTS``.

        Nothing is written to QSettings until the user clicks OK — this is
        a non-destructive preview so they can still Cancel out.
        """
        confirm = QMessageBox.question(
            self,
            "Reset Settings",
            "Reset every Settings field to its default value?\n\n"
            "Your changes won't be saved until you click OK.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Repopulate widgets from _DEFAULTS. Auto-detect blender/everything
        # paths since they're left blank by default.
        defaults = config._DEFAULTS
        self.blender_exe.setText(
            defaults.get("blender_exe", "") or self._auto_detect_blender()
        )
        self.everything_dll.setText(
            defaults.get("everything_dll", "") or self._auto_detect_everything()
        )
        self.presets_path.setText(defaults.get("presets_path", ""))
        self.cue4parse_cli.setText(defaults.get("cue4parse_cli", ""))
        self.addon_name.setText(defaults.get("psk_addon_name", ""))
        self.timeout.setValue(int(defaults.get("timeout_seconds", 120)))
        self.export_threads.setValue(int(defaults.get("unpacker_export_threads", 0)))
        self.physical_memory_only.setChecked(bool(defaults.get("unpacker_physical_memory_only", False)))
        self.memory_limit.setValue(int(defaults.get("unpacker_memory_limit_percent", 0)))
        self.gpu_accel.setCurrentText(str(defaults.get("unpacker_gpu_accel", "auto")).capitalize())
        self.texture_format.setCurrentText(defaults.get("export_texture_format", "png"))
        self.webp_mode.setCurrentText(defaults.get("export_texture_webp_mode", "lossless"))
        self.webp_quality.setValue(int(defaults.get("export_texture_webp_quality", 90)))
        self.audio_format.setCurrentText(defaults.get("export_audio_format", "wav"))
        self.mesh_format.setCurrentText(defaults.get("export_mesh_format", "psk"))
        self.ui_scale.setValue(int(defaults.get("ui_scale", 100)))

    # ------------------------------------------------------------------
    # Appearance shortcut
    # ------------------------------------------------------------------

    def _reload_theme_combo(self):
        """Repopulate the dropdown — the Customize dialog can add/delete
        custom schemes while Settings stays open."""
        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        names = list_scheme_names()
        self.theme_combo.addItems(names)
        active = config.get("color_scheme") or theme.current_scheme_name()
        if active in names:
            self.theme_combo.setCurrentText(active)
        self.theme_combo.blockSignals(False)

    def _on_theme_selected(self, name: str):
        # Applies immediately, matching the Customize dialog's behavior —
        # theme choice is not gated on the OK button.
        config.set("color_scheme", name)
        app = QApplication.instance()
        if app:
            theme.apply(app, name)
        self.settings_changed.emit()

    def _open_color_scheme_dialog(self):
        dlg = ColorSchemeDialog(self)
        dlg.scheme_changed.connect(self._on_scheme_chosen)
        dlg.exec()

    def _on_scheme_chosen(self, name: str):
        # Theme is already applied and persisted by ColorSchemeDialog; sync
        # the dropdown and emit so any outer listeners refresh.
        self._reload_theme_combo()
        self.settings_changed.emit()

    def _open_presets(self):
        path = self.presets_path.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(
                self, "Presets file not found",
                f"Could not open texture presets:\n{path or '(no path set)'}",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _run_tests(self):
        """Test all configured paths and SDK connections."""
        self._test_output.clear()
        results: list[tuple[str, bool, str]] = []  # (test_name, passed, detail)

        # 1. Active profile's game folder (sourced from config — set by the
        # Manage Profiles dialog and on profile load).
        gf = config.get("game_folder")
        if gf and os.path.isdir(gf):
            results.append(("Active profile · Game folder", True, gf))
        elif gf:
            results.append(("Active profile · Game folder", False, f"Directory not found: {gf}"))
        else:
            results.append((
                "Active profile · Game folder",
                False,
                "Not set — open Manage Profiles to configure",
            ))

        # 2. Blender exe
        be = self.blender_exe.text()
        if be and os.path.isfile(be):
            # Try running blender --version
            import subprocess
            try:
                proc = subprocess.run(
                    [be, "--version"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                ver_line = proc.stdout.strip().splitlines()[0] if proc.stdout else "Unknown"
                results.append(("Blender", True, ver_line))
            except Exception as e:
                results.append(("Blender", False, f"Exe exists but failed to run: {e}"))
        elif be:
            results.append(("Blender", False, f"File not found: {be}"))
        else:
            results.append(("Blender", False, "Not set"))

        # 3. Everything SDK (with WalkSearcher fallback)
        dll_path = self.everything_dll.text()
        everything_ipc_ok = False
        if dll_path and os.path.isfile(dll_path):
            results.append(("Everything DLL", True, f"Found: {dll_path}"))
            try:
                from core.everything import EverythingSDK, reset_sdk
                reset_sdk()
                sdk = EverythingSDK(dll_path)

                # Test IPC connection
                ok, msg = sdk.test_connection()
                everything_ipc_ok = ok
                results.append(("Everything IPC", ok, msg))

                # Test folder search if active profile's game folder is set
                if ok and gf and os.path.isdir(gf):
                    count, msg = sdk.test_folder_search(gf)
                    results.append(("Folder Search", count > 0, msg))

                    # Try finding mesh files. Zero hits is not a failure — a
                    # packed/original game folder has no meshes until they
                    # are exported from the Unpacker.
                    mesh_files = sdk.find_mesh_files(folder=gf)
                    if mesh_files:
                        results.append((
                            "Mesh Search",
                            True,
                            f"Found {len(mesh_files)} mesh files "
                            f"(psk/pskx/glb/fbx), e.g.: {mesh_files[0].name}",
                        ))
                    else:
                        results.append((
                            "Mesh Search",
                            True,
                            "No mesh files (psk/pskx/glb/fbx) yet — normal for "
                            "a packed game folder; they appear after exporting "
                            "meshes",
                        ))
            except Exception as e:
                results.append(("Everything SDK", False, f"Error: {e}"))
        elif dll_path:
            results.append(("Everything DLL", False, f"File not found: {dll_path}"))
        else:
            results.append(("Everything DLL", False, "Not set"))

        if not everything_ipc_ok:
            results.append((
                "File Search Fallback",
                True,
                "Using built-in walker — slower than Everything but works offline. "
                "Install/launch Everything (https://www.voidtools.com/) for faster scans.",
            ))

        # 5. Presets JSON
        pp = self.presets_path.text()
        if pp and os.path.isfile(pp):
            import json
            try:
                with open(pp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                preset_names = list(data.get("presets", {}).keys())
                results.append(("Presets JSON", True, f"Presets: {', '.join(preset_names)}"))
            except Exception as e:
                results.append(("Presets JSON", False, f"Parse error: {e}"))
        elif pp:
            results.append(("Presets JSON", False, f"File not found: {pp}"))
        else:
            results.append(("Presets JSON", False, "Not set"))

        # 6. CUE4Parse CLI
        cli_path = self.cue4parse_cli.text()
        if cli_path and os.path.isfile(cli_path):
            import subprocess
            try:
                proc = subprocess.run(
                    [cli_path, "--version"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                ver_line = proc.stdout.strip().splitlines()[0] if proc.stdout else "Unknown"
                results.append(("CUE4Parse CLI", True, ver_line))
            except Exception as e:
                results.append(("CUE4Parse CLI", False, f"Exe exists but failed to run: {e}"))
        elif cli_path:
            results.append(("CUE4Parse CLI", False, f"File not found: {cli_path}"))
        else:
            results.append(("CUE4Parse CLI", False, "Not set (optional)"))

        # Render results
        lines: list[str] = []
        all_pass = True
        for name, passed, detail in results:
            icon = "PASS" if passed else "FAIL"
            if not passed:
                all_pass = False
            lines.append(f"[{icon}] {name}: {detail}")

        lines.append("")
        if all_pass:
            lines.append("All tests passed!")
        else:
            lines.append("Some tests failed — check paths above.")

        self._test_output.setPlainText("\n".join(lines))

    @staticmethod
    def _auto_detect_blender() -> str:
        from core import tools_detect
        return tools_detect.detect_blender()

    @staticmethod
    def _auto_detect_everything() -> str:
        from core import tools_detect
        return tools_detect.detect_everything_dll()
