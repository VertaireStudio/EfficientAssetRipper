"""Tests for `gui.settings_panel.SettingsDialog` (mocks QFileDialog).

After the per-profile path refactor, SettingsDialog only carries global
tooling paths: Blender Executable, Everything SDK DLL, Texture Presets JSON,
CUE4Parse CLI. Per-profile paths (game folder / mounted folder / output
folder) live in the Manage Profiles dialog and are exercised in
``test_profile_dialog.py``.
"""

from __future__ import annotations

import os

import pytest

import config
from gui.widgets import PathPicker
from gui.settings_panel import SettingsDialog

pytestmark = [pytest.mark.qt, pytest.mark.gui]


def test_path_picker_text_round_trip(qtbot):
    p = PathPicker(mode="folder")
    qtbot.addWidget(p)
    p.setText(r"C:\Picked")
    assert p.text() == r"C:\Picked"


def test_path_picker_browse_uses_qfiledialog_static_mock(qtbot, monkeypatch):
    """Browse should call QFileDialog.getExistingDirectory in folder mode."""
    from PySide6.QtWidgets import QFileDialog

    captured: dict = {}

    def _fake(parent, title, start=""):
        captured["called"] = (parent, title, start)
        return r"C:\From\Mock"

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(_fake))
    p = PathPicker(mode="folder")
    qtbot.addWidget(p)
    p._browse()
    assert captured.get("called") is not None
    assert p.text() == r"C:\From\Mock"


def test_settings_dialog_constructs(qtbot, mock_qsettings):
    """Building the SettingsDialog with stubbed config should not raise."""
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "Settings"


def test_settings_dialog_load_populates_fields_from_config(qtbot, mock_qsettings):
    config.set("blender_exe", r"C:\Blender\blender.exe")
    config.set("psk_addon_name", "addon.from.config")
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    assert dlg.blender_exe.text() == r"C:\Blender\blender.exe"
    assert dlg.addon_name.text() == "addon.from.config"


def test_settings_dialog_save_writes_to_config(qtbot, mock_qsettings, monkeypatch):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.blender_exe.setText(r"C:\Blender\NEW\blender.exe")
    dlg.addon_name.setText("custom.addon.name")
    dlg.timeout.setValue(300)

    # Validation prompts on missing paths — auto-confirm "Save anyway".
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )

    with qtbot.waitSignal(dlg.settings_changed, timeout=1000):
        dlg._save()

    assert config.get("blender_exe") == r"C:\Blender\NEW\blender.exe"
    assert config.get("psk_addon_name") == "custom.addon.name"
    assert config.get_int("timeout_seconds") == 300


def test_settings_dialog_export_format_combos(qtbot, mock_qsettings):
    """The format combos must expose every supported export format."""
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    assert [dlg.texture_format.itemText(i) for i in range(dlg.texture_format.count())] == [
        "png", "tga", "webp"]
    assert [dlg.audio_format.itemText(i) for i in range(dlg.audio_format.count())] == [
        "wav", "ogg", "mp3", "webm"]
    assert [dlg.mesh_format.itemText(i) for i in range(dlg.mesh_format.count())] == [
        "psk", "glb", "fbx"]


def test_settings_dialog_webp_widgets_enabled_state(qtbot, mock_qsettings):
    """WebP mode/quality widgets gate on the selected texture format."""
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    # Default texture format is png — WebP tuning disabled…
    assert not dlg.webp_mode.isEnabled()
    dlg.texture_format.setCurrentText("webp")
    assert dlg.webp_mode.isEnabled()
    # …but the quality spin only matters in lossy mode.
    assert not dlg.webp_quality.isEnabled()
    dlg.webp_mode.setCurrentText("lossy")
    assert dlg.webp_quality.isEnabled()


def test_settings_dialog_save_persists_webp_and_mesh_format(qtbot, mock_qsettings, monkeypatch):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.texture_format.setCurrentText("webp")
    dlg.webp_mode.setCurrentText("lossy")
    dlg.webp_quality.setValue(85)
    dlg.audio_format.setCurrentText("mp3")
    dlg.mesh_format.setCurrentText("glb")

    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    dlg._save()

    assert config.get("export_texture_format") == "webp"
    assert config.get("export_texture_webp_mode") == "lossy"
    assert config.get_int("export_texture_webp_quality") == 85
    assert config.get("export_audio_format") == "mp3"
    assert config.get("export_mesh_format") == "glb"


def test_settings_dialog_save_aborts_on_missing_path_confirm_no(qtbot, mock_qsettings, monkeypatch):
    """Saying No to the missing-path prompt should abort the save."""
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.blender_exe.setText(r"C:\Nope\blender.exe")
    config.set("blender_exe", r"C:\Old\blender.exe")
    config.set("psk_addon_name", "old.addon")

    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )

    dlg._save()

    # Old value should remain untouched because save was aborted.
    assert config.get("blender_exe") == r"C:\Old\blender.exe"
    assert config.get("psk_addon_name") == "old.addon"


def test_settings_dialog_does_not_expose_per_profile_path_fields(qtbot, mock_qsettings):
    """Per-profile path fields must not appear on the global Settings dialog."""
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    for removed in ("game_folder", "output_dir", "unpack_output_dir"):
        assert not hasattr(dlg, removed), (
            f"SettingsDialog still exposes {removed!r}; should be in Manage Profiles"
        )


def test_reset_to_defaults_repopulates_widgets_without_saving(qtbot, mock_qsettings, monkeypatch):
    """Reset is a non-destructive widget refresh — config stays untouched
    until OK is clicked."""
    from PySide6.QtWidgets import QMessageBox
    config.set("psk_addon_name", "stale.addon.name")
    config.set("timeout_seconds", 1234)  # in-range so the slider doesn't clamp
    config.set("export_texture_format", "tga")
    config.set("export_mesh_format", "fbx")

    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    # Sanity: the dialog loaded the stale values.
    assert dlg.addon_name.text() == "stale.addon.name"
    assert dlg.timeout.value() == 1234
    assert dlg.texture_format.currentText() == "tga"

    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    dlg._reset_to_defaults()

    # Widgets show defaults now…
    assert dlg.addon_name.text() == config._DEFAULTS["psk_addon_name"]
    assert dlg.timeout.value() == config._DEFAULTS["timeout_seconds"]
    assert dlg.texture_format.currentText() == config._DEFAULTS["export_texture_format"]
    assert dlg.mesh_format.currentText() == config._DEFAULTS["export_mesh_format"]
    # …but config still holds the stale values until the user clicks OK.
    assert config.get("psk_addon_name") == "stale.addon.name"
    assert config.get_int("timeout_seconds") == 1234


def test_reset_to_defaults_aborts_on_no(qtbot, mock_qsettings, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    config.set("psk_addon_name", "stay.put")
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )
    dlg._reset_to_defaults()
    assert dlg.addon_name.text() == "stay.put"


def test_theme_dropdown_applies_scheme_immediately(qtbot, mock_qsettings):
    """Picking a theme in the Appearance dropdown persists and applies it
    without going through the Customize Colors dialog or OK."""
    import gui.theme as theme

    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    with qtbot.waitSignal(dlg.settings_changed, timeout=1000):
        dlg.theme_combo.setCurrentText("Slate")
    assert config.get("color_scheme") == "Slate"
    assert theme.current_scheme_name() == "Slate"


def test_theme_dropdown_lists_all_schemes(qtbot, mock_qsettings):
    from gui.color_schemes import list_scheme_names

    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    items = [dlg.theme_combo.itemText(i) for i in range(dlg.theme_combo.count())]
    assert items == list_scheme_names()


def test_open_presets_warns_on_missing_path(qtbot, mock_qsettings, monkeypatch):
    """Opening a non-existent presets file should warn instead of crashing."""
    from PySide6.QtWidgets import QMessageBox

    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.presets_path.setText(r"C:\Nope\does_not_exist.json")

    captured: dict = {}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **kw: captured.setdefault("called", True)),
    )

    dlg._open_presets()
    assert captured.get("called") is True


def test_settings_dialog_timeout_clamped(qtbot, mock_qsettings):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    # The slider range is 10..3600, mirroring the old spin box
    dlg.timeout.setValue(99999)
    assert dlg.timeout.value() <= 3600
    dlg.timeout.setValue(-50)
    assert dlg.timeout.value() >= 10


# ---------------------------------------------------------------------------
# Timeout / Export Threads — sliders carrying the spin box's semantics
# ---------------------------------------------------------------------------

def test_timeout_and_threads_sliders_keep_spinbox_semantics(qtbot, mock_qsettings):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)

    # Timeout: range preserved, live caption includes the unit.
    assert dlg.timeout.minimum() == 10
    assert dlg.timeout.maximum() == 3600
    assert dlg.timeout.value() == config._DEFAULTS["timeout_seconds"]
    assert dlg.timeout.label_text == "120 seconds"

    # Export threads: the 0 stop reads "Auto (all cores)" like before.
    assert dlg.export_threads.minimum() == 0
    assert dlg.export_threads.maximum() == max(1, os.cpu_count() or 64)
    assert dlg.export_threads.specialValueText() == "Auto (all cores)"
    assert dlg.export_threads.label_text == "Auto (all cores)"
    dlg.export_threads.setValue(4)
    assert dlg.export_threads.value() == 4
    assert dlg.export_threads.label_text == "4"


def test_export_threads_slider_caps_at_cpu_thread_count(qtbot, mock_qsettings, monkeypatch):
    """The top stop tracks this machine's thread count, not a fixed 64."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    assert dlg.export_threads.maximum() == 8
    dlg.export_threads.setValue(8)       # top stop = full machine
    assert dlg.export_threads.value() == 8
    # An over-committed stored value clamps onto the detected cap.
    dlg.export_threads.setValue(16)
    assert dlg.export_threads.value() == 8
    dlg.export_threads.setValue(0)
    assert dlg.export_threads.label_text == "Auto (all cores)"


# ---------------------------------------------------------------------------
# Export memory policy — checkbox + percent spinbox (0 = Unlimited)
# ---------------------------------------------------------------------------

def test_memory_policy_widgets_exist_with_defaults(qtbot, mock_qsettings):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)

    assert dlg.physical_memory_only.isChecked() is False
    assert dlg.memory_limit.minimum() == 0
    assert dlg.memory_limit.maximum() == 95
    assert dlg.memory_limit.value() == 0
    assert dlg.memory_limit.specialValueText() == "Unlimited"
    assert dlg.memory_limit.label_text == "Unlimited"
    # No policy set → no absolute-budget hint is shown.
    assert dlg.memory_hint.text() == ""


def test_memory_policy_hint_shows_budget(qtbot, mock_qsettings, monkeypatch):
    from core import memory_info
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(memory_info, "available_bytes", lambda: 8 * 1024**3)

    dlg = SettingsDialog()
    qtbot.addWidget(dlg)

    dlg.memory_limit.setValue(50)
    dlg.physical_memory_only.setChecked(False)
    # 50% of the 8 GB currently free → 4.0 GB of 8.0 GB.
    assert "4.0 GB of 8.0 GB" in dlg.memory_hint.text()
    assert dlg.memory_limit.label_text == "50 %"

    # Physical-only flips on → hint mentions the clamp.
    dlg.physical_memory_only.setChecked(True)
    assert dlg.memory_hint.text()


def test_memory_policy_save_persists(qtbot, mock_qsettings, monkeypatch):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.physical_memory_only.setChecked(True)
    dlg.memory_limit.setValue(75)

    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    dlg._save()

    assert config.get_raw("unpacker_physical_memory_only") is True
    assert config.get_int("unpacker_memory_limit_percent") == 75


def test_memory_policy_reset_restores_defaults(qtbot, mock_qsettings, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.physical_memory_only.setChecked(True)
    dlg.memory_limit.setValue(75)
    dlg._reset_to_defaults()

    assert dlg.physical_memory_only.isChecked() is False
    assert dlg.memory_limit.value() == 0
