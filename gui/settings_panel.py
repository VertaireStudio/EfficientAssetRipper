"""Settings dialog for configuring global paths, timeouts, presets, and appearance.

Per-profile paths (game folder, mounted folder, output folder) live in the
profile JSON and are edited via the Manage Profiles dialog — this dialog only
covers global tooling paths shared across every profile.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
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
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config
from gui.color_scheme_dialog import ColorSchemeDialog
from gui.color_schemes import list_scheme_names
from gui.widgets import CollapsibleSection, PathPicker
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
            mode="file", filter_str="Blender (blender.exe);;All Files (*)",
            title="Select Blender Executable",
        )
        self.blender_exe.setText(
            config.get("blender_exe") or self._auto_detect_blender()
        )
        paths_form.addRow("Blender Executable:", self.blender_exe)

        self.everything_dll = PathPicker(
            mode="file", filter_str="DLL Files (*.dll);;All Files (*)",
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

        self.cue4parse_cli = PathPicker(
            mode="file", filter_str="Executable (CUE4ParseCLI.exe);;All Files (*)",
            title="Select CUE4Parse CLI",
        )
        self.cue4parse_cli.setText(config.get("cue4parse_cli"))
        paths_form.addRow("CUE4Parse CLI:", self.cue4parse_cli)

        # --- Processing group ---
        proc_form = QFormLayout()

        self.addon_name = QLineEdit(config.get("psk_addon_name"))
        proc_form.addRow("PSK Import Addon:", self.addon_name)

        self.timeout = QSpinBox()
        self.timeout.setRange(10, 3600)
        self.timeout.setSuffix(" seconds")
        self.timeout.setValue(config.get_int("timeout_seconds") or 120)
        proc_form.addRow("Timeout per Asset:", self.timeout)

        # --- Export Formats group ---
        fmt_section = CollapsibleSection("Export Formats", start_expanded=True)
        fmt_form = QFormLayout()

        self.texture_format = QComboBox()
        self.texture_format.addItems(["png", "tga"])
        self.texture_format.setCurrentText(config.get("export_texture_format"))
        fmt_form.addRow("Textures:", self.texture_format)

        self.audio_format = QComboBox()
        self.audio_format.addItems(["wav", "ogg"])
        self.audio_format.setCurrentText(config.get("export_audio_format"))
        fmt_form.addRow("Audio:", self.audio_format)

        mesh_label = QLabel("PSK / PSKX (automatic)")
        fmt_form.addRow("Meshes:", mesh_label)

        fmt_section.set_content_layout(fmt_form)
        layout.addWidget(fmt_section)

        # --- Presets shortcut (one-shot action, not a section) ---
        presets_btn = QPushButton("Open Texture Presets JSON in Editor")
        presets_btn.clicked.connect(self._open_presets)
        presets_row = QHBoxLayout()
        presets_row.addStretch()
        presets_row.addWidget(presets_btn)
        presets_row.addStretch()
        layout.addLayout(presets_row)

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
        self.ui_scale = QSpinBox()
        self.ui_scale.setRange(theme.UI_SCALE_MIN, theme.UI_SCALE_MAX)
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
        # Clamp the timeout into a sane range (the QSpinBox already constrains
        # via setRange, but be defensive in case the constraint changes).
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
        config.set("cue4parse_cli", self.cue4parse_cli.text())
        config.set("export_texture_format", self.texture_format.currentText())
        config.set("export_audio_format", self.audio_format.currentText())

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
        self.texture_format.setCurrentText(defaults.get("export_texture_format", "png"))
        self.audio_format.setCurrentText(defaults.get("export_audio_format", "wav"))
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

                    # Try finding PSK files. Zero hits is not a failure — a
                    # packed/original game folder has no PSKs until meshes
                    # are exported from the Unpacker.
                    psk_files = sdk.find_psk_files(folder=gf)
                    if psk_files:
                        results.append((
                            "PSK Search",
                            True,
                            f"Found {len(psk_files)} PSK/PSKX files, e.g.: {psk_files[0].name}",
                        ))
                    else:
                        results.append((
                            "PSK Search",
                            True,
                            "No PSK/PSKX files yet — normal for a packed game "
                            "folder; they appear after exporting meshes",
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
        candidates = [
            r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return ""

    @staticmethod
    def _auto_detect_everything() -> str:
        candidates = [
            r"C:\Program Files\Everything\Everything64.dll",
            r"C:\Program Files (x86)\Everything\Everything64.dll",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return ""
