"""Asset browser with tree-based category grouping, search, and filtering.

Replaces the flat table with a Category > Subcategory > Asset tree for
navigating 40K+ assets.  Supports checkbox selection, text filtering,
category filtering, status filtering, and a detail dialog.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from core.asset_scanner import AssetEntry
from gui.widgets import CollapsibleSection, ZoomableTree, add_tree_expand_actions
import gui.theme as theme


# ---------------------------------------------------------------------------
# Detail dialog
# ---------------------------------------------------------------------------

class AssetDetailDialog(QDialog):
    """Shows full material/texture resolution info for one asset."""

    rescan_requested = Signal(object)  # emits the AssetEntry
    reprocess_requested = Signal(object)  # emits the AssetEntry

    def __init__(self, asset: AssetEntry, parent=None):
        super().__init__(parent)
        self._asset = asset
        self.setWindowTitle(f"Asset Detail \u2014 {asset.name}")
        self.setMinimumSize(700, 500)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{asset.name}</b>"))
        path_label = QLabel(f"Path: {asset.psk_path}")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)

        # Clickable blend path — always shown right after Path
        if asset.blend_path:
            if asset.blend_path.is_file():
                blend_label = QLabel(
                    f'Blend: <a href="open">{asset.blend_path}</a>'
                )
                blend_label.setWordWrap(True)
                blend_label.setCursor(Qt.CursorShape.PointingHandCursor)
                blend_label.linkActivated.connect(
                    lambda _: self._open_blend(asset.blend_path)
                )
                layout.addWidget(blend_label)
            else:
                missing_label = QLabel(f"Blend: {asset.blend_path} (file missing)")
                missing_label.setWordWrap(True)
                missing_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(missing_label)

        cat_label = QLabel(f"Category: {asset.category} / {asset.subcategory}")
        if (
            asset.category == "Uncategorized"
            and asset.subcategory == "Unknown"
        ):
            cat_label.setText(
                f"Category: {asset.category} / {asset.subcategory}  "
                f"⚠ Asset path is not under the configured Game Folder — check Manage Profiles"
            )
            cat_label.setToolTip(
                f"PSK path: {asset.psk_path}\n"
                f"Configured game folder did not match — see logs for details."
            )
        cat_label.setWordWrap(True)
        layout.addWidget(cat_label)
        layout.addWidget(QLabel(f"Status: {asset.status_text}"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        # Collect all unresolved/missing across all materials for summary section
        all_unresolved: list[tuple[str, str, str]] = []  # (mat_name, tex_name, reason)

        for mat in asset.materials:
            group = CollapsibleSection(
                f"{mat.slot_name}  ({mat.material_name})"
            )
            form = QFormLayout()

            if not mat.props_found:
                form.addRow("", QLabel("\u26a0 Material .props.txt not found"))

            form.addRow("Preset:", QLabel(mat.preset_used))

            fallback_slots = set(mat.keyword_fallback_used or [])
            for tex in mat.textures:
                marker = "\u2713"
                suffix = ""
                if tex.slot in fallback_slots:
                    marker = "\u24d8"  # circled-i \u2014 visually flags "guessed"
                    suffix = "  (auto-detected)"
                label = QLabel(f"{marker} {tex.path}{suffix}")
                label.setWordWrap(True)
                if tex.slot in fallback_slots:
                    label.setToolTip(
                        "This texture was filled by the keyword auto-detect "
                        "fallback because suffix matching produced nothing. "
                        "Add a per-material override if it's wrong."
                    )
                form.addRow(f"{tex.slot} ({tex.colorspace}):", label)

            for utex in mat.unresolved:
                label = QLabel(f"\u2717 {utex.texture_name} \u2014 {utex.reason}")
                form.addRow("Missing:", label)
                all_unresolved.append(
                    (mat.material_name, utex.texture_name, utex.reason)
                )

            if mat.bsdf_overrides:
                overrides_str = ", ".join(
                    f"{k}={v}" for k, v in mat.bsdf_overrides.items()
                )
                form.addRow("BSDF overrides:", QLabel(overrides_str))

            group.set_content_layout(form)
            scroll_layout.addWidget(group)

        # Unresolved / missing textures summary section
        if all_unresolved:
            unresolved_group = CollapsibleSection(
                f"Unresolved Textures ({len(all_unresolved)})",
                start_expanded=True,
            )
            uform = QFormLayout()
            for mat_name, tex_name, reason in all_unresolved:
                label = QLabel(f"\u2717 {tex_name} \u2014 {reason}")
                uform.addRow(f"[{mat_name}]:", label)
            unresolved_group.set_content_layout(uform)
            scroll_layout.addWidget(unresolved_group)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        # Bottom button row
        btn_row = QHBoxLayout()

        rescan_btn = QPushButton("Re-scan This Asset")
        rescan_btn.clicked.connect(self._on_rescan)
        btn_row.addWidget(rescan_btn)

        reprocess_btn = QPushButton("Reprocess This Asset")
        reprocess_btn.setToolTip("Re-run Blender processing (overwrites existing .blend)")
        reprocess_btn.clicked.connect(self._on_reprocess)
        btn_row.addWidget(reprocess_btn)

        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def _on_rescan(self):
        self.rescan_requested.emit(self._asset)
        self.close()

    def _on_reprocess(self):
        self.reprocess_requested.emit(self._asset)
        self.close()

    @staticmethod
    def _open_blend(blend_path):
        from PySide6.QtWidgets import QMessageBox

        blender_exe = config.get("blender_exe")
        if not blender_exe:
            QMessageBox.warning(
                None, "Blender Not Configured",
                "Set the Blender executable in Settings before opening .blend files.",
            )
            return
        if not Path(blender_exe).is_file():
            QMessageBox.warning(
                None, "Blender Not Found",
                f"Configured Blender path doesn't exist:\n{blender_exe}",
            )
            return
        try:
            subprocess.Popen([blender_exe, str(blend_path)])
        except OSError as e:
            QMessageBox.warning(None, "Failed to launch Blender", str(e))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def _status_colors():
    c = theme.current_scheme()
    return {
        "ready":            QColor(c["success"]),
        "missing_textures": QColor(c["warning"]),
        "no_props":         QColor(c["error"]),
        "no_materials":     QColor(c["error"]),
        "processed":        QColor(c["info"]),
    }


_STATUS_LABELS = {
    "all":              "All Statuses",
    "ready":            "Ready",
    "processed":        "Processed",
    "missing_textures": "Missing Textures",
    "no_props":         "No Props",
    "no_materials":     "No Materials",
}


# ---------------------------------------------------------------------------
# Asset browser widget
# ---------------------------------------------------------------------------

class AssetBrowser(QWidget):
    """Tree-based asset browser grouped by category/subcategory."""

    selection_changed = Signal(list)
    rescan_requested = Signal(list)  # list[AssetEntry] to re-resolve
    add_to_queue_requested = Signal(list)  # list[AssetEntry] to queue
    reprocess_requested = Signal(object)  # single AssetEntry to reprocess
    delete_requested = Signal(list)  # list[AssetEntry] to remove from cache
    mesh_preview_requested = Signal(object)   # AssetEntry — open in Mesh Preview tab
    props_view_requested = Signal(object)     # AssetEntry — open .props.txt in Text Viewer
    scan_requested = Signal()                 # user clicked Scan Game Folder
    cancel_scan_requested = Signal()          # user clicked Cancel Scan

    def __init__(self, parent=None):
        super().__init__(parent)
        self._assets: list[AssetEntry] = []
        self._item_to_idx: dict[int, int] = {}

        # Debounce filter-driven tree rebuilds — typing triggers up to ~10
        # rebuilds/sec on a 40k-asset folder otherwise.
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(150)
        self._filter_debounce.timeout.connect(self._rebuild_tree)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Search row: name filter + advanced toggle ---
        search_row = QHBoxLayout()

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name...")
        self._search.textChanged.connect(self._schedule_rebuild)
        search_row.addWidget(self._search, 1)

        layout.addLayout(search_row)

        # --- Advanced filters (collapsed by default) ---
        adv_section = CollapsibleSection("Advanced filters", start_expanded=False)
        adv_layout = QHBoxLayout()
        adv_layout.setContentsMargins(0, 2, 0, 2)

        adv_layout.addWidget(QLabel("Category:"))
        self._cat_filter = QComboBox()
        self._cat_filter.addItem("All Categories")
        self._cat_filter.currentIndexChanged.connect(self._rebuild_tree)
        self._cat_filter.setMinimumWidth(150)
        adv_layout.addWidget(self._cat_filter, 1)

        adv_layout.addWidget(QLabel("Status:"))
        self._status_filter = QComboBox()
        for key, label in _STATUS_LABELS.items():
            self._status_filter.addItem(label, key)
        self._status_filter.currentIndexChanged.connect(self._rebuild_tree)
        self._status_filter.setMinimumWidth(130)
        adv_layout.addWidget(self._status_filter, 1)

        adv_section.set_content_layout(adv_layout)
        layout.addWidget(adv_section)

        # --- Middle area: add to queue ---
        # Tracks nodes we auto-expanded so we can restore them when the filter
        # is cleared without re-opening anything the user manually collapsed.
        self._auto_expanded: set[int] = set()
        self._user_collapsed: set[int] = set()
        self._last_filter_active = False

        btn_bar = QHBoxLayout()

        self._scan_btn = QPushButton("Scan Game Folder")
        self._scan_btn.setToolTip("Walk the configured game folder and resolve every PSK/PSKX asset")
        self._scan_btn.clicked.connect(self.scan_requested.emit)
        btn_bar.addWidget(self._scan_btn)

        self._cancel_scan_btn = QPushButton("Cancel Scan")
        self._cancel_scan_btn.setEnabled(False)
        self._cancel_scan_btn.clicked.connect(self.cancel_scan_requested.emit)
        btn_bar.addWidget(self._cancel_scan_btn)

        btn_bar.addStretch()

        self._add_queue_btn = QPushButton("Add to Queue")
        self._add_queue_btn.setToolTip("Add checked assets to the processing queue")
        self._add_queue_btn.clicked.connect(self._request_add_to_queue)
        self._add_queue_btn.setProperty("cssClass", "accent")
        btn_bar.addWidget(self._add_queue_btn)

        layout.addLayout(btn_bar)

        # --- Tree ---
        self._tree = ZoomableTree()
        self._tree.setHeaderLabels(["Name", "Status", "Materials", "Blend File", "Path"])
        self._tree.setColumnWidth(0, 350)
        self._tree.setColumnWidth(1, 160)
        self._tree.setColumnWidth(2, 80)
        self._tree.setColumnWidth(3, 200)
        self._tree.setColumnWidth(4, 300)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.doubleClicked.connect(self._on_double_click)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree)

        # --- Status bar ---
        self._status = QLabel("")
        layout.addWidget(self._status)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_assets(self, assets: list[AssetEntry]):
        """Populate the browser with discovered assets."""
        self._assets = assets
        self._populate_category_filter()
        self._rebuild_tree()

    def get_assets(self) -> list[AssetEntry]:
        """Return all loaded assets."""
        return list(self._assets)

    @property
    def assets(self) -> list[AssetEntry]:
        """Public accessor for the underlying asset list (read-only by convention)."""
        return self._assets

    def _schedule_rebuild(self):
        """Coalesce rapid filter changes into a single tree rebuild."""
        self._filter_debounce.start()

    def get_selected_assets(self) -> list[AssetEntry]:
        """Return list of checked (ticked) assets."""
        selected: list[AssetEntry] = []
        self._walk_leaves(self._tree.invisibleRootItem(), selected)
        return selected

    # ------------------------------------------------------------------
    # Internal: build tree
    # ------------------------------------------------------------------

    def _populate_category_filter(self):
        """Fill category dropdown from actual asset data."""
        self._cat_filter.blockSignals(True)
        self._cat_filter.clear()
        self._cat_filter.addItem("All Categories")

        cats: dict[str, int] = defaultdict(int)
        for a in self._assets:
            cats[a.category] += 1

        for cat in sorted(cats):
            self._cat_filter.addItem(f"{cat} ({cats[cat]})", cat)

        self._cat_filter.blockSignals(False)

    def _rebuild_tree(self):
        """Rebuild the tree from current filters."""
        self._tree.clear()
        self._item_to_idx.clear()

        filter_text = self._search.text().lower().strip()
        cat_data = self._cat_filter.currentData()
        status_data = self._status_filter.currentData() or "all"

        groups: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )

        visible_count = 0
        ready_count = 0

        for idx, asset in enumerate(self._assets):
            if filter_text and filter_text not in asset.name.lower():
                continue
            if cat_data and asset.category != cat_data:
                continue
            if status_data != "all" and asset.status != status_data:
                continue

            groups[asset.category][asset.subcategory].append(idx)
            visible_count += 1
            if asset.status == "ready":
                ready_count += 1

        self._tree.setUpdatesEnabled(False)

        # Hoist the per-leaf status-colour lookup so a 40k-asset rebuild
        # doesn't allocate a fresh dict + N QColors per leaf.
        status_color_map = _status_colors()

        for cat_name in sorted(groups):
            cat_item = QTreeWidgetItem(self._tree)
            cat_item.setText(0, cat_name)
            sub_count = sum(len(v) for v in groups[cat_name].values())
            cat_item.setText(1, f"({sub_count} assets)")
            cat_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsAutoTristate
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            cat_item.setCheckState(0, Qt.CheckState.Unchecked)
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)

            for sub_name in sorted(groups[cat_name]):
                indices = groups[cat_name][sub_name]
                sub_item = QTreeWidgetItem(cat_item)
                sub_item.setText(0, sub_name)
                sub_item.setText(1, f"({len(indices)} assets)")
                sub_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsAutoTristate
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                sub_item.setCheckState(0, Qt.CheckState.Unchecked)

                for asset_idx in indices:
                    asset = self._assets[asset_idx]
                    leaf = QTreeWidgetItem(sub_item)
                    leaf.setText(0, asset.name)
                    leaf.setText(1, asset.status_text)
                    mat_names = ", ".join(m.material_name for m in asset.materials) if asset.materials else ""
                    leaf.setText(2, mat_names)
                    if asset.blend_path:
                        leaf.setText(3, str(asset.blend_path))
                    leaf.setText(4, str(asset.psk_path.parent))

                    leaf.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    leaf.setCheckState(0, Qt.CheckState.Unchecked)

                    color = status_color_map.get(asset.status)
                    if color:
                        leaf.setForeground(1, color)

                    self._item_to_idx[id(leaf)] = asset_idx

        filter_active = bool(filter_text) or bool(cat_data) or status_data != "all"
        if filter_active:
            self._tree.expandAll()
            self._auto_expanded = {id(self._tree.topLevelItem(i)) for i in range(self._tree.topLevelItemCount())}
        elif self._last_filter_active:
            # Filter just cleared — collapse anything we auto-expanded.
            self._tree.collapseAll()
            self._auto_expanded.clear()
        self._last_filter_active = filter_active

        self._tree.setUpdatesEnabled(True)

        self._status.setText(
            f"{visible_count} assets shown ({ready_count} ready) "
            f"\u2014 {len(self._assets)} total"
        )

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _walk_leaves(self, parent: QTreeWidgetItem, out: list[AssetEntry]):
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.childCount() > 0:
                self._walk_leaves(child, out)
            else:
                if child.checkState(0) == Qt.CheckState.Checked:
                    idx = self._item_to_idx.get(id(child))
                    if idx is not None:
                        out.append(self._assets[idx])

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        menu = QMenu(self)

        idx = self._item_to_idx.get(id(item)) if item is not None else None

        if idx is not None:
            asset = self._assets[idx]

            act_mesh = QAction("Preview Mesh", self)
            act_mesh.setEnabled(asset.psk_path.is_file())
            if not act_mesh.isEnabled():
                act_mesh.setToolTip("PSK file is missing on disk")
            act_mesh.triggered.connect(lambda: self.mesh_preview_requested.emit(asset))
            menu.addAction(act_mesh)

            act_props = QAction("Preview Properties", self)
            props_path = asset.psk_path.with_suffix(".props.txt")
            act_props.setEnabled(props_path.is_file())
            if not act_props.isEnabled():
                act_props.setToolTip("No .props.txt next to this PSK")
            act_props.triggered.connect(lambda: self.props_view_requested.emit(asset))
            menu.addAction(act_props)

            menu.addSeparator()

            act_delete = QAction("Remove from list / cache", self)
            act_delete.triggered.connect(lambda: self._delete_assets([asset]))
            menu.addAction(act_delete)

        add_tree_expand_actions(menu, self._tree, item)

        if menu.actions():
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Left-click on a leaf row → open its .props.txt in the Text Viewer.

        Category / subcategory rows have no entry in `_item_to_idx` and are
        ignored. Clicking the checkbox column is unchanged — Qt routes that
        through the item's own check-state machinery, which we don't intercept.
        """
        idx = self._item_to_idx.get(id(item))
        if idx is None:
            return
        self.props_view_requested.emit(self._assets[idx])

    def _delete_assets(self, assets: list[AssetEntry]):
        to_remove = {str(a.psk_path) for a in assets}
        self._assets = [a for a in self._assets if str(a.psk_path) not in to_remove]
        self._populate_category_filter()
        self._rebuild_tree()
        self.delete_requested.emit(assets)

    # ------------------------------------------------------------------
    # Detail view
    # ------------------------------------------------------------------

    def _on_double_click(self, index):
        # Prefer the QModelIndex argument over currentItem() so a true
        # double-click reaches the correct row even if focus shifts.
        item = self._tree.itemFromIndex(index) if index.isValid() else self._tree.currentItem()
        if item is None:
            return
        idx = self._item_to_idx.get(id(item))
        if idx is not None:
            dlg = AssetDetailDialog(self._assets[idx], parent=self)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.rescan_requested.connect(
                lambda asset: self.rescan_requested.emit([asset])
            )
            dlg.reprocess_requested.connect(
                lambda asset: self.reprocess_requested.emit(asset)
            )
            dlg.exec()

    def _request_add_to_queue(self):
        """Emit add_to_queue_requested with checked assets."""
        selected = self.get_selected_assets()
        if selected:
            self.add_to_queue_requested.emit(selected)

    def refresh_tree(self):
        """Rebuild the tree to reflect updated asset data (e.g. after re-scan)."""
        self._rebuild_tree()

    def set_scan_running(self, running: bool) -> None:
        """Toggle the scan / cancel-scan button enabled state."""
        self._scan_btn.setEnabled(not running)
        self._cancel_scan_btn.setEnabled(running)
