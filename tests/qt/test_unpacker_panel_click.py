"""Tests for single-click export listing on "+" package rows."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.unpacker_panel import UnpackerPanel

pytestmark = pytest.mark.qt


@pytest.fixture
def panel(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    return p


def _add_row(panel, name: str, *, is_folder: bool = False):
    panel._on_browse_result("", [{"name": name, "is_folder": is_folder}])
    item = panel._tree.invisibleRootItem().child(0)
    return item


def test_single_click_on_package_lists_exports(panel, monkeypatch):
    item = _add_row(panel, "Hero.uasset")
    called = []
    monkeypatch.setattr(panel._unpacker, "list_exports", called.append)

    panel._on_item_clicked(item, 0)
    assert called == ["Hero.uasset"]

    # Second click (and the double-click that follows any double click's
    # first click) must not refetch — the in-progress flag gates it.
    panel._on_item_clicked(item, 0)
    panel._on_item_double_clicked(item, 0)
    assert called == ["Hero.uasset"]


def test_single_click_on_folder_or_non_package_is_a_noop(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel._unpacker, "list_exports", called.append)

    folder = _add_row(panel, "Meshes", is_folder=True)
    panel._on_item_clicked(folder, 0)

    panel._tree.clear()
    texture = _add_row(panel, "Skin.tga")
    panel._on_item_clicked(texture, 0)

    assert called == []


def test_modified_click_stays_a_selection_gesture(panel, monkeypatch):
    item = _add_row(panel, "Hero.uasset")
    called = []
    monkeypatch.setattr(panel._unpacker, "list_exports", called.append)
    monkeypatch.setattr(
        QApplication, "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )

    panel._on_item_clicked(item, 0)
    assert called == []
