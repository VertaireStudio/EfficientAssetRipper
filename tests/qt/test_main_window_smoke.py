"""Smoke tests for `gui.main_window.MainWindow` construction + menu shape."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.qt, pytest.mark.gui]


@pytest.fixture
def main_window(qtbot, mock_qsettings, tmp_profiles_dir):
    """Construct MainWindow with isolated settings + profiles. Not shown."""
    # Mark first-run setup as complete so the wizard doesn't fire during tests.
    import config
    config.set("setup_complete", "1")
    from gui.main_window import MainWindow
    win = MainWindow()
    qtbot.addWidget(win)
    return win


def test_main_window_constructs_with_default_config(main_window):
    # Title may include the active profile name appended (e.g. "EfficientAssetRipper — Default")
    assert main_window.windowTitle().startswith("EfficientAssetRipper")


def test_main_window_has_profiles_menu(main_window):
    """Profile selection lives in the menu bar — not a header bar widget."""
    titles = [
        a.menu().title().replace("&", "")
        for a in main_window.menuBar().actions() if a.menu()
    ]
    assert "Profiles" in titles
    assert main_window._profiles_menu is not None


def test_main_window_creates_queue_panel(main_window):
    assert main_window._queue is not None
    assert main_window._queue._table.rowCount() == 0


def test_main_window_creates_log_viewer(main_window):
    assert main_window._log is not None


def test_menu_actions_present(main_window):
    # Menu titles include `&` mnemonics; strip them for the comparison.
    menu_titles = [
        a.menu().title().replace("&", "")
        for a in main_window.menuBar().actions() if a.menu()
    ]
    assert "File" in menu_titles
    assert "Profiles" in menu_titles
    assert "Tools" in menu_titles
    assert "Help" in menu_titles
    assert "Window" in menu_titles


def test_help_menu_has_setup_wizard_action(main_window):
    """Help → Run Setup Wizard... should re-arm the wizard."""
    help_menu = None
    for a in main_window.menuBar().actions():
        if a.menu() and a.menu().title().replace("&", "") == "Help":
            help_menu = a.menu()
            break
    assert help_menu is not None
    titles = [a.text().replace("&", "") for a in help_menu.actions() if a.text()]
    assert any("Setup Wizard" in t for t in titles)


def test_status_bar_initial_message(main_window):
    msg = main_window._statusbar.currentMessage()
    # Either the standard ready message, or the Blender-missing warning when
    # the test environment doesn't have Blender installed.
    assert (
        "Ready" in msg
        or "Configure" in msg
        or "configure" in msg
        or "Blender" in msg
    )


def test_left_docks_have_browser_picker_unpacker(main_window):
    """Each left-side panel exists as a QDockWidget with a stable objectName."""
    object_names = {dock.objectName() for dock in main_window._docks.values()}
    assert {"dock_asset_browser", "dock_psk_picker", "dock_unpacker"}.issubset(object_names)


def test_queue_log_and_combiner_docks_exist(main_window):
    object_names = {dock.objectName() for dock in main_window._docks.values()}
    assert "dock_queue" in object_names
    assert "dock_log" in object_names
    assert "dock_blend_combiner" in object_names


def test_docks_are_not_floatable(main_window):
    """In-window docking only — tearing out as an OS window is intentionally disabled."""
    from PySide6.QtWidgets import QDockWidget
    floatable = QDockWidget.DockWidgetFeature.DockWidgetFloatable
    for dock in main_window._docks.values():
        assert not bool(dock.features() & floatable), (
            f"{dock.objectName()} has DockWidgetFloatable enabled"
        )


def test_grouped_dragging_enabled(main_window):
    """Qt only allows dragging a tab out of a tabbed dock group when the
    GroupedDragging dock option is set — without it tab drags are ignored."""
    from PySide6.QtWidgets import QMainWindow
    assert bool(main_window.dockOptions() & QMainWindow.DockOption.GroupedDragging)


def test_is_busy_false_initially(main_window):
    assert main_window._is_busy() is False


def test_default_profile_seeded_on_first_launch(main_window, tmp_profiles_dir):
    """First launch with no profiles should auto-create a Default profile."""
    profiles = list(tmp_profiles_dir.glob("*.json"))
    assert len(profiles) >= 1
    assert any(p.stem == "Default" for p in profiles)


def test_unpacker_handoff_routes_through_resolver(main_window, monkeypatch):
    """Regression: extracted PSKs from the Unpacker must go through the
    resolve worker, not be added as empty stubs.

    Previously ``_on_psks_extracted`` built ``AssetEntry`` stubs with
    ``materials=[]`` and added them straight to the queue/browser. When the
    user then clicked Process, the manifest had ``materials: {}`` and every
    Blender material warned "No texture spec for material: X (available: [])".
    The fix delegates to ``_add_picker_to_queue`` so each PSK gets resolved
    before it lands in the queue.
    """
    from pathlib import Path

    captured: list[list] = []

    def fake_add(paths):
        # Record what was forwarded; don't actually spin up scanner / SDK.
        captured.append(list(paths))

    monkeypatch.setattr(main_window, "_add_picker_to_queue", fake_add)

    psks = [Path("F:/fake/A.pskx"), Path("F:/fake/B.pskx")]
    main_window._on_psks_extracted(psks)

    assert captured == [psks], (
        "Unpacker hand-off should delegate to _add_picker_to_queue so the "
        "resolve worker runs; got captured=%r" % captured
    )

    # Empty input is a no-op (must not invoke the picker path).
    captured.clear()
    main_window._on_psks_extracted([])
    assert captured == []


def test_is_busy_skips_workers_with_deleted_cpp_object(main_window):
    """Regression: profile switch crashed with `RuntimeError: Internal C++
    object (_PickerResolveWorker) already deleted` when a finished worker's
    Qt object was destroyed before the finished-slot pruned the entry from
    ``_active_workers``. ``_is_busy`` (called by ProfileBar's busy_check)
    iterates that list, so the dead reference blew up the dropdown.
    """

    class DeadWorker:
        """Stand-in for a QThread whose C++ side has been deleted.

        ``isRunning()`` mimics the PySide6 RuntimeError; ``cancel`` is
        intentionally absent so any caller that only does ``getattr(w, 'cancel')``
        is unaffected.
        """

        def isRunning(self):  # noqa: N802 — Qt API name
            raise RuntimeError(
                "Internal C++ object (_PickerResolveWorker) already deleted"
            )

    class LiveRunningWorker:
        def isRunning(self):  # noqa: N802
            return True

    class LiveIdleWorker:
        def isRunning(self):  # noqa: N802
            return False

    dead = DeadWorker()
    running = LiveRunningWorker()
    idle = LiveIdleWorker()

    main_window._active_workers = [dead, running, idle]

    # Should not raise, and should report busy because of the live runner.
    assert main_window._is_busy() is True
    # Dead entry must have been pruned so it can't trip the next caller.
    assert dead not in main_window._active_workers
    assert running in main_window._active_workers
    assert idle in main_window._active_workers

    # With only dead + idle workers, _is_busy returns False (and prunes dead).
    main_window._active_workers = [DeadWorker(), LiveIdleWorker()]
    # Force the pre-built `_unpacker_panel`/`_job_manager` to be inert so
    # _is_busy's other branches don't influence the result.
    main_window._unpacker_panel._exporting = False
    main_window._job_manager = None
    main_window._pending_cache_writes = 0

    assert main_window._is_busy() is False
    # Dead entry pruned again.
    assert all(not isinstance(w, DeadWorker) for w in main_window._active_workers)
