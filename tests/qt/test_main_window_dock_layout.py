"""Dock layout persistence + Reset Layout for MainWindow."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDockWidget

import config

pytestmark = [pytest.mark.qt, pytest.mark.gui]


@pytest.fixture
def main_window(qtbot, mock_qsettings, tmp_profiles_dir):
    config.set("setup_complete", "1")
    from gui.main_window import MainWindow
    win = MainWindow()
    qtbot.addWidget(win)
    # show() required so QDockWidget visibility / toggleViewAction.isChecked()
    # reflect the *layout* state rather than the unrendered "no parent shown"
    # state. The Qt platform is forced to "offscreen" in tests/qt/conftest.py
    # so this never produces a visible window.
    # show() required so QDockWidget visibility / toggleViewAction.isChecked()
    # reflect the *layout* state rather than the unrendered "no parent shown"
    # state. Offscreen QPA platform makes this invisible.
    win.show()
    qtbot.wait(0)  # let pending Qt events flush so dock state stabilises
    return win


def test_first_run_falls_back_to_default_layout(qtbot, mock_qsettings, tmp_profiles_dir):
    """No saved state in QSettings → default layout applies. Blend Combiner
    and the TGA/Media previews start hidden by design; everything else open."""
    config.set("setup_complete", "1")
    from gui.main_window import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    win.show()  # required so dock visibility / toggleViewAction.isChecked() are realised
    qtbot.wait(0)

    hidden_by_default = {"dock_blend_combiner", "dock_tga_previewer", "dock_media_previewer"}
    for dock in win._docks.values():
        expected = dock.objectName() not in hidden_by_default
        assert dock.toggleViewAction().isChecked() is expected, (
            f"{dock.objectName()} visibility != {expected} in the default layout"
        )


def test_save_then_restore_round_trip(main_window):
    """saveLayout writes to config; restoreLayout returns True with that state."""
    win = main_window
    win._save_layout()
    schema = config.get(win._LAYOUT_KEY_SCHEMA)
    assert int(schema or 0) == win._LAYOUT_SCHEMA_VERSION
    assert config.get(win._LAYOUT_KEY_GEOMETRY)
    assert config.get(win._LAYOUT_KEY_STATE)

    # A subsequent restore must accept the blob we just wrote.
    assert win._restore_layout() is True


def test_schema_mismatch_rejects_saved_state(main_window):
    """A stale schema version causes restoreLayout to refuse the blob."""
    win = main_window
    win._save_layout()
    config.set(win._LAYOUT_KEY_SCHEMA, win._LAYOUT_SCHEMA_VERSION + 100)

    assert win._restore_layout() is False


def test_reset_layout_action_clears_persisted_state_and_reapplies_defaults(main_window):
    """Reset Layout reopens every closed dock and wipes the saved blob."""
    win = main_window
    # Close a dock via its toggle action (mirrors the user clicking the X).
    toggle = win._asset_browser_dock.toggleViewAction()
    toggle.setChecked(False)
    win._save_layout()
    assert toggle.isChecked() is False

    win._reset_dock_layout()

    assert win._asset_browser_dock.toggleViewAction().isChecked() is True
    # Persisted blob must be cleared so the next launch starts from defaults.
    assert int(config.get(win._LAYOUT_KEY_SCHEMA) or 0) == 0
    assert (config.get(win._LAYOUT_KEY_GEOMETRY) or "") == ""


def test_corrupt_geometry_blob_falls_back_to_defaults(main_window):
    """A junk base64 blob in QSettings should not crash restore."""
    win = main_window
    config.set(win._LAYOUT_KEY_SCHEMA, win._LAYOUT_SCHEMA_VERSION)
    config.set(win._LAYOUT_KEY_GEOMETRY, "***not-base64***")
    config.set(win._LAYOUT_KEY_STATE, "***not-base64***")

    # Should return False (defaults applied by caller); no exception escapes.
    assert win._restore_layout() is False


def test_window_menu_contains_reset_layout(main_window):
    win = main_window
    window_menu = None
    for a in win.menuBar().actions():
        if a.menu() and a.menu().title().replace("&", "") == "Window":
            window_menu = a.menu()
            break
    assert window_menu is not None
    titles = [a.text().replace("&", "") for a in window_menu.actions() if a.text()]
    assert any("Reset Layout" in t for t in titles)


def test_window_menu_dock_toggles_match_dock_visibility(main_window):
    """Each dock_specs entry has a corresponding visibility toggle in the Window menu."""
    win = main_window
    window_menu = None
    for a in win.menuBar().actions():
        if a.menu() and a.menu().title().replace("&", "") == "Window":
            window_menu = a.menu()
            break
    assert window_menu is not None

    menu_labels = {
        a.text().replace("&", "")
        for a in window_menu.actions()
        if a.text() and not a.isSeparator()
    }
    for _name, menu_label, _title, _w in win._dock_specs:
        assert menu_label in menu_labels, f"missing Window menu entry for {menu_label}"
