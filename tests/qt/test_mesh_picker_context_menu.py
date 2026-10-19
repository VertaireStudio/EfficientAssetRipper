"""Tests for the Mesh Picker's right-click context menu."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu, QTreeWidgetItem

from gui.mesh_picker import MeshPickerPanel

pytestmark = pytest.mark.qt


# The shared expand/collapse helper appends these to every tree menu.
_EXPAND_ACTION_LABELS = {
    "Expand All",
    "Collapse All",
    "Expand Selected",
    "Collapse Selected",
}


def _capture_menu_actions(panel: MeshPickerPanel, item: QTreeWidgetItem) -> list[str]:
    """Drive `_popup_context_menu` and return the picker-specific action labels.

    Filters out the shared Expand/Collapse helper entries so existing
    assertions stay focused on the preview / reveal options.
    """
    captured: list[list[str]] = []
    orig_init = QMenu.__init__

    def hooked(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        def _rec(*a, **k):
            labels: list[str] = []
            for action in self.actions():
                text = action.text()
                if action.isSeparator():
                    continue
                if text in _EXPAND_ACTION_LABELS:
                    continue
                labels.append(text)
            captured.append(labels)
            return None
        self.exec = _rec  # type: ignore[assignment]

    QMenu.__init__ = hooked  # type: ignore[assignment]
    try:
        panel._popup_context_menu(item, QPoint(0, 0))
    finally:
        QMenu.__init__ = orig_init  # type: ignore[assignment]
    return captured[0] if captured else []


def _seed_picker(panel: MeshPickerPanel, paths: list[Path]) -> list[QTreeWidgetItem]:
    """Populate the picker with given paths and return the leaf tree items."""
    panel._all_paths = list(paths)
    panel._categories = [("TestCat", "TestSub") for _ in paths]
    panel._rebuild_tree()
    leaves: list[QTreeWidgetItem] = []
    root = panel._tree.invisibleRootItem()
    for ci in range(root.childCount()):
        cat = root.child(ci)
        for si in range(cat.childCount()):
            sub = cat.child(si)
            for li in range(sub.childCount()):
                leaves.append(sub.child(li))
    return leaves


def test_menu_for_leaf_offers_preview_and_reveal(qtbot, tmp_path: Path):
    panel = MeshPickerPanel()
    qtbot.addWidget(panel)
    psk = tmp_path / "Mesh.psk"
    psk.write_bytes(b"")
    leaves = _seed_picker(panel, [psk])
    assert leaves, "expected at least one leaf item after rebuild"

    actions = _capture_menu_actions(panel, leaves[0])
    assert actions == ["Preview Mesh (PSK)", "Open containing folder"]


def test_menu_skipped_for_category_header(qtbot, tmp_path: Path):
    """Category/subcategory headers aren't in `_item_to_idx`; the menu builder
    bails before any QMenu actions are added."""
    panel = MeshPickerPanel()
    qtbot.addWidget(panel)
    psk = tmp_path / "Mesh.psk"
    psk.write_bytes(b"")
    _seed_picker(panel, [psk])

    cat_item = panel._tree.invisibleRootItem().child(0)
    actions = _capture_menu_actions(panel, cat_item)
    assert actions == []


def test_preview_action_emits_signal_with_psk_path(qtbot, tmp_path: Path):
    panel = MeshPickerPanel()
    qtbot.addWidget(panel)
    psk = tmp_path / "Mesh.psk"
    psk.write_bytes(b"")
    leaves = _seed_picker(panel, [psk])

    # Build the menu, find "Preview Mesh", trigger it, assert signal payload.
    captured: list[QMenu] = []
    orig_init = QMenu.__init__

    def hooked(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        captured.append(self)
        self.exec = lambda *a, **k: None  # type: ignore[assignment]

    QMenu.__init__ = hooked  # type: ignore[assignment]
    try:
        with qtbot.waitSignal(panel.mesh_preview_requested, timeout=2000) as blocker:
            panel._popup_context_menu(leaves[0], QPoint(0, 0))
            menu = captured[0]
            preview_action = next(
                a for a in menu.actions() if a.text().startswith("Preview Mesh")
            )
            preview_action.trigger()
    finally:
        QMenu.__init__ = orig_init  # type: ignore[assignment]

    assert blocker.args[0] == str(psk)


def test_reveal_action_opens_parent_folder_via_qdesktopservices(qtbot, tmp_path: Path):
    panel = MeshPickerPanel()
    qtbot.addWidget(panel)
    psk = tmp_path / "sub" / "Mesh.psk"
    psk.parent.mkdir(parents=True, exist_ok=True)
    psk.write_bytes(b"")
    leaves = _seed_picker(panel, [psk])

    captured: list[QMenu] = []
    orig_init = QMenu.__init__

    def hooked(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        captured.append(self)
        self.exec = lambda *a, **k: None  # type: ignore[assignment]

    QMenu.__init__ = hooked  # type: ignore[assignment]
    try:
        with patch.object(QDesktopServices, "openUrl") as mock_open:
            panel._popup_context_menu(leaves[0], QPoint(0, 0))
            menu = captured[0]
            reveal_action = next(
                a for a in menu.actions() if a.text() == "Open containing folder"
            )
            reveal_action.trigger()
    finally:
        QMenu.__init__ = orig_init  # type: ignore[assignment]

    assert mock_open.called
    url = mock_open.call_args[0][0]
    # QUrl.fromLocalFile produces file:// URLs; just check the local path.
    assert Path(url.toLocalFile()) == psk.parent
