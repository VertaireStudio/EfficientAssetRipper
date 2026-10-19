"""Mesh file picker — search and select specific mesh files before processing.

Searches every supported mesh export format together (PSK/PSKX straight from
CUE4Parse, .glb from the "glb" mesh-format setting, .fbx from the Blender
conversion worker) via the Everything SDK without running full material
resolution.  Selected files are resolved on-demand when added to the
processing queue.

Uses tree-based Category > Subcategory grouping (same layout as the Asset
Browser) so that the user can navigate, search, and filter large file lists.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from core.classifier import classify
from core.everything import EverythingError, MESH_EXTS, get_sdk
from gui.widgets import CollapsibleSection, ZoomableTree, add_tree_expand_actions
import gui.theme as theme


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _format_display(path: Path) -> str:
    """Short uppercase label for the Format column ("PSK", "PSKX", ...)."""
    return path.suffix.lstrip(".").upper()


def _matches_format_filter(path: Path, fmt_key: str) -> bool:
    """True when *path*'s extension belongs to combo key *fmt_key*.

    "psk" covers both .psk and .pskx (same binary family); "glb"/"fbx"
    match exactly.
    """
    sfx = path.suffix.lower().lstrip(".")
    if fmt_key == "psk":
        return sfx in ("psk", "pskx")
    return sfx == fmt_key


# ---------------------------------------------------------------------------
# Background worker – fetches filenames from Everything
# ---------------------------------------------------------------------------

class MeshSearchWorker(QThread):
    """Background thread that queries Everything for mesh files."""

    finished = Signal(list)   # list[Path]
    error = Signal(str)

    def __init__(self, game_folder: str, dll_path: str | None, parent=None):
        super().__init__(parent)
        self._game_folder = game_folder
        self._dll_path = dll_path

    def run(self):
        try:
            sdk = get_sdk(self._dll_path)
            results = sdk.find_mesh_files(folder=self._game_folder)
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Picker widget
# ---------------------------------------------------------------------------

class MeshPickerPanel(QWidget):
    """Tree-based mesh file picker grouped by category/subcategory.

    Lists PSK/PSKX, GLB and FBX files together in one tree; a Format filter
    narrows the view when the user wants a single export format.
    """

    add_to_queue_requested = Signal(list)  # list[Path] – raw mesh paths
    mesh_preview_requested = Signal(str)   # absolute mesh file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_paths: list[Path] = []
        self._categories: list[tuple[str, str]] = []  # parallel to _all_paths
        self._item_to_idx: dict[int, int] = {}
        self._processed_paths: set[str] = set()  # paths already processed
        self._checked_paths: set[str] = set()    # persist selections across rebuilds
        self._current_game_folder: str = ""      # detect game folder switches
        self._worker: MeshSearchWorker | None = None
        # Optional callback that yields the live export root (the Unpacker
        # tab's Mounted Folder). main_window injects it so the picker follows
        # field edits without bridge syncs.
        self._search_root_provider: Callable[[], str] | None = None

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

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Name filter — space=AND, -term=exclude  e.g. rig -lod -shadow")
        self._filter.setToolTip(
            "Filter by filename. Rules:\n"
            "  term1 term2   — file must contain ALL terms (AND)\n"
            "  -term         — file must NOT contain this term\n"
            "Multiple terms can be mixed, e.g.:  rig -lod -shadow"
        )
        self._filter.textChanged.connect(self._schedule_rebuild)
        search_row.addWidget(self._filter, 1)

        layout.addLayout(search_row)

        # --- Advanced filters (collapsed by default) ---
        adv_section = CollapsibleSection("Advanced filters", start_expanded=False)
        adv_layout = QVBoxLayout()
        adv_layout.setContentsMargins(0, 2, 0, 2)

        adv_row1 = QHBoxLayout()
        adv_row1.addWidget(QLabel("Folder:"))
        self._folder_filter = QLineEdit()
        self._folder_filter.setPlaceholderText("Folder path filter — e.g. characters -npcs")
        self._folder_filter.setToolTip(
            "Filter by parent folder path. Same syntax as name filter:\n"
            "  term1 term2   — path must contain ALL terms\n"
            "  -term         — path must NOT contain this term"
        )
        self._folder_filter.textChanged.connect(self._schedule_rebuild)
        adv_row1.addWidget(self._folder_filter, 3)

        adv_row1.addWidget(QLabel("Category:"))
        self._cat_filter = QComboBox()
        self._cat_filter.addItem("All Categories")
        self._cat_filter.currentIndexChanged.connect(self._rebuild_tree)
        self._cat_filter.setMinimumWidth(150)
        adv_row1.addWidget(self._cat_filter, 1)
        adv_layout.addLayout(adv_row1)

        adv_row2 = QHBoxLayout()
        adv_row2.addWidget(QLabel("Sort:"))
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Name A→Z", "Name Z→A", "Folder A→Z", "Folder Z→A"])
        self._sort_combo.currentIndexChanged.connect(self._rebuild_tree)
        self._sort_combo.setMinimumWidth(120)
        adv_row2.addWidget(self._sort_combo)

        adv_row2.addWidget(QLabel("Format:"))
        self._format_filter = QComboBox()
        self._format_filter.addItem("All Formats")
        for ext in ("psk", "glb", "fbx"):
            label = {"psk": "PSK/PSKX", "glb": "GLB", "fbx": "FBX"}[ext]
            self._format_filter.addItem(label, ext)
        self._format_filter.currentIndexChanged.connect(self._rebuild_tree)
        self._format_filter.setMinimumWidth(100)
        self._format_filter.setToolTip(
            "Narrow the tree to one mesh export format.\n"
            "PSK/PSKX covers both Unreal PSK binary variants."
        )
        adv_row2.addWidget(self._format_filter)

        self._unprocessed_only = QCheckBox("Unprocessed only")
        self._unprocessed_only.setToolTip("Hide files that have already been processed")
        self._unprocessed_only.toggled.connect(self._rebuild_tree)
        adv_row2.addWidget(self._unprocessed_only)
        adv_row2.addStretch()
        adv_layout.addLayout(adv_row2)

        adv_section.set_content_layout(adv_layout)
        layout.addWidget(adv_section)

        # --- Middle area: re-load + add to queue ---
        # Tracks last-known filter activity so clearing the filter collapses
        # the rows we auto-expanded without disturbing user-driven expansions.
        self._last_filter_active = False

        btn_bar = QHBoxLayout()

        self._search_btn = QPushButton("Re-load Meshes")
        self._search_btn.setToolTip(
            "Re-fetch all mesh filenames (PSK/PSKX, GLB, FBX) from Everything SDK"
        )
        self._search_btn.clicked.connect(self._start_search)
        btn_bar.addWidget(self._search_btn)

        btn_bar.addStretch()

        self._add_btn = QPushButton("Add to Queue")
        self._add_btn.setToolTip("Resolve selected files and add to the processing queue")
        self._add_btn.clicked.connect(self._request_add)
        self._add_btn.setProperty("cssClass", "accent")
        btn_bar.addWidget(self._add_btn)

        layout.addLayout(btn_bar)

        # --- Tree ---
        self._tree = ZoomableTree()
        self._tree.setHeaderLabels(["Name", "Folder", "Format"])
        self._tree.setColumnWidth(0, 340)
        self._tree.setColumnWidth(1, 340)
        self._tree.setColumnWidth(2, 60)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree)

        # --- Status ---
        status_bar = QHBoxLayout()
        self._status = QLabel("")
        status_bar.addWidget(self._status)
        status_bar.addStretch()
        self._sel_count = QLabel("")
        status_bar.addWidget(self._sel_count)
        layout.addLayout(status_bar)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def set_search_root_provider(self, provider: Callable[[], str]) -> None:
        """Inject the live export-root resolver (Unpacker Mounted Folder)."""
        self._search_root_provider = provider

    def _resolve_search_root(self) -> str:
        """Live export root → profile-loaded folder → config fallbacks."""
        if self._search_root_provider is not None:
            try:
                root = self._search_root_provider()
            except Exception:  # noqa: BLE001 — provider issues are callers'
                root = ""
            if root:
                return root
        if self._current_game_folder:
            return self._current_game_folder
        return config.get("unpack_output_dir") or config.get("game_folder")

    def _start_search(self):
        search_folder = self._resolve_search_root()
        if not search_folder:
            QMessageBox.warning(self, "No Search Folder",
                                "Set an unpack output directory in the profile first.")
            return

        dll_path = config.get("everything_dll") or None
        self._search_btn.setEnabled(False)
        self._status.setText("Searching...")

        # Reap the previous backend object so repeated re-loads don't
        # accumulate QThread instances parented to this panel (leak).
        old = self._worker
        self._worker = None
        if old is not None:
            try:
                running = old.isRunning()
            except RuntimeError:
                # Its queued deleteLater already ran — C++ object is gone,
                # the Python wrapper is just a husk. Nothing to clean up.
                running = False
            if running:
                # Can't be freed while its thread runs; keep it parented and
                # let its finish signal clean it up instead.
                old.finished.connect(old.deleteLater)
                old.error.connect(old.deleteLater)
            else:
                try:
                    old.deleteLater()
                except RuntimeError:
                    pass  # already destroyed

        self._worker = MeshSearchWorker(search_folder, dll_path, self)
        self._worker.finished.connect(self._on_search_done)
        self._worker.error.connect(self._on_search_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker.start()

    @Slot(list)
    def _on_search_done(self, paths: list[Path]):
        self._search_btn.setEnabled(True)
        # Drop the reference before its queued deleteLater destroys the C++
        # object, so a fast "Re-load Meshes" never touches a dead wrapper.
        self._worker = None
        self._all_paths = sorted(paths, key=lambda p: p.stem.lower())

        # Classify every path
        game_folder = self._current_game_folder or ""
        self._categories = []
        for p in self._all_paths:
            cat = classify(p, game_folder)
            self._categories.append((cat.category, cat.subcategory))

        self._populate_category_filter()
        self._filter.clear()
        self._folder_filter.clear()
        self._rebuild_tree()
        self._status.setText(
            f"{len(self._all_paths)} mesh files found "
            f"({', '.join(e.upper() for e in MESH_EXTS)})"
        )

    @Slot(str)
    def _on_search_error(self, error: str):
        self._search_btn.setEnabled(True)
        # Same lifecycle rule as _on_search_done — drop before destruction.
        self._worker = None
        self._status.setText(f"Error: {error}")

    # ------------------------------------------------------------------
    # Category filter population
    # ------------------------------------------------------------------

    def _populate_category_filter(self):
        self._cat_filter.blockSignals(True)
        self._cat_filter.clear()
        self._cat_filter.addItem("All Categories")

        cats: dict[str, int] = defaultdict(int)
        for cat, _sub in self._categories:
            cats[cat] += 1

        for cat in sorted(cats):
            self._cat_filter.addItem(f"{cat} ({cats[cat]})", cat)

        self._cat_filter.blockSignals(False)

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_filter(text: str) -> tuple[list[str], list[str]]:
        """Parse a filter string into (include_terms, exclude_terms).

        Space-separated tokens; tokens starting with '-' are excludes.
        """
        include_terms: list[str] = []
        exclude_terms: list[str] = []
        for token in text.lower().split():
            if token.startswith("-") and len(token) > 1:
                exclude_terms.append(token[1:])
            elif token:
                include_terms.append(token)
        return include_terms, exclude_terms

    @staticmethod
    def _matches_filter(
        value: str, include_terms: list[str], exclude_terms: list[str]
    ) -> bool:
        """Return True if value satisfies all include/exclude terms."""
        v = value.lower()
        if any(t not in v for t in include_terms):
            return False
        if any(t in v for t in exclude_terms):
            return False
        return True

    # ------------------------------------------------------------------
    # Build tree
    # ------------------------------------------------------------------

    def _schedule_rebuild(self):
        """Coalesce rapid filter changes into a single tree rebuild."""
        self._filter_debounce.start()

    def _rebuild_tree(self):
        self._tree.blockSignals(True)
        self._tree.clear()
        self._item_to_idx.clear()

        name_inc, name_exc = self._parse_filter(self._filter.text())
        folder_inc, folder_exc = self._parse_filter(self._folder_filter.text())
        cat_data = self._cat_filter.currentData()
        fmt_data = self._format_filter.currentData()
        unprocessed_only = self._unprocessed_only.isChecked()

        sort_idx = self._sort_combo.currentIndex()
        # 0=Name A-Z, 1=Name Z-A, 2=Folder A-Z, 3=Folder Z-A

        groups: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )

        visible_count = 0
        for idx, p in enumerate(self._all_paths):
            if unprocessed_only and str(p) in self._processed_paths:
                continue
            if not self._matches_filter(p.stem, name_inc, name_exc):
                continue
            if (folder_inc or folder_exc) and not self._matches_filter(
                str(p.parent), folder_inc, folder_exc
            ):
                continue
            cat, sub = self._categories[idx]
            if cat_data and cat != cat_data:
                continue
            if fmt_data and not _matches_format_filter(p, fmt_data):
                continue
            groups[cat][sub].append(idx)
            visible_count += 1

        # Sort indices within each subcategory group
        def _sort_key(i: int):
            p = self._all_paths[i]
            if sort_idx == 0:
                return p.stem.lower()
            elif sort_idx == 1:
                return p.stem.lower()  # reversed below
            elif sort_idx in (2, 3):
                return (str(p.parent).lower(), p.stem.lower())
            return p.stem.lower()

        for cat_name in groups:
            for sub_name in groups[cat_name]:
                groups[cat_name][sub_name].sort(
                    key=_sort_key,
                    reverse=(sort_idx in (1, 3)),
                )

        self._tree.setUpdatesEnabled(False)

        for cat_name in sorted(groups):
            cat_item = QTreeWidgetItem(self._tree)
            cat_item.setText(0, cat_name)
            sub_count = sum(len(v) for v in groups[cat_name].values())
            cat_item.setText(1, f"({sub_count} files)")
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
                sub_item.setText(1, f"({len(indices)} files)")
                sub_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsAutoTristate
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                sub_item.setCheckState(0, Qt.CheckState.Unchecked)

                for file_idx in indices:
                    p = self._all_paths[file_idx]
                    pkey = str(p)
                    leaf = QTreeWidgetItem(sub_item)
                    leaf.setText(0, p.name)
                    leaf.setText(1, str(p.parent))
                    leaf.setText(2, _format_display(p))

                    leaf.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    checked = pkey in self._checked_paths
                    leaf.setCheckState(
                        0,
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                    )
                    if pkey in self._processed_paths:
                        processed_color = QColor(theme.current_scheme()["info"])
                        leaf.setForeground(0, processed_color)
                        leaf.setForeground(1, processed_color)
                        leaf.setForeground(2, processed_color)

                    self._item_to_idx[id(leaf)] = file_idx

        filter_active = (
            bool(name_inc or name_exc or folder_inc or folder_exc)
            or bool(cat_data)
            or bool(fmt_data)
            or unprocessed_only
        )
        if filter_active:
            self._tree.expandAll()
        elif self._last_filter_active:
            self._tree.collapseAll()
        self._last_filter_active = filter_active

        self._tree.setUpdatesEnabled(True)
        self._tree.blockSignals(False)
        self._status.setText(
            f"{visible_count} shown / {len(self._all_paths)} total"
        )
        self._update_sel_count()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Track check-state changes in _checked_paths."""
        if column != 0:
            return
        idx = self._item_to_idx.get(id(item))
        if idx is None:
            return
        pkey = str(self._all_paths[idx])
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked_paths.add(pkey)
        else:
            self._checked_paths.discard(pkey)
        self._update_sel_count()

    def _update_sel_count(self):
        n = len(self._checked_paths)
        self._sel_count.setText(f"{n} selected" if n else "")

    def _show_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        self._popup_context_menu(item, self._tree.viewport().mapToGlobal(pos))

    def _popup_context_menu(self, item: QTreeWidgetItem | None, global_pos):
        """Build and show the right-click menu for *item*. Split out from
        `_show_context_menu` so tests can drive it directly without poking
        the QTreeWidget's internal hit-testing."""
        menu = QMenu(self)

        idx = self._item_to_idx.get(id(item)) if item is not None else None
        if idx is not None:
            mesh_path = self._all_paths[idx]

            act_preview = QAction(f"Preview Mesh ({_format_display(mesh_path)})", self)
            act_preview.triggered.connect(
                lambda checked=False, p=mesh_path: self.mesh_preview_requested.emit(str(p))
            )
            menu.addAction(act_preview)

            act_reveal = QAction("Open containing folder", self)
            act_reveal.triggered.connect(
                lambda checked=False, p=mesh_path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(p.parent))
                )
            )
            menu.addAction(act_reveal)

        add_tree_expand_actions(menu, self._tree, item)

        if menu.actions():
            menu.exec(global_pos)

    def _walk_leaves(self, parent: QTreeWidgetItem, out: list[int]):
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.childCount() > 0:
                self._walk_leaves(child, out)
            else:
                if child.checkState(0) == Qt.CheckState.Checked:
                    idx = self._item_to_idx.get(id(child))
                    if idx is not None:
                        out.append(idx)

    def _get_checked_paths(self) -> list[Path]:
        indices: list[int] = []
        self._walk_leaves(self._tree.invisibleRootItem(), indices)
        return [self._all_paths[i] for i in indices]

    def _request_add(self):
        paths = self._get_checked_paths()
        if paths:
            self.add_to_queue_requested.emit(paths)
        else:
            self._status.setText("No files checked — check some files first")

    # ------------------------------------------------------------------
    # Public: game folder awareness + mark paths as queued / processed
    # ------------------------------------------------------------------

    def refresh_for_game(self, game_folder: str):
        """Auto-load mesh files. Resets state if the game folder changed."""
        if not game_folder:
            return
        if game_folder != self._current_game_folder:
            # Different game — wipe all per-game state so stale paths don't linger
            self._all_paths = []
            self._categories = []
            self._item_to_idx = {}
            self._processed_paths.clear()
            self._checked_paths.clear()
            self._current_game_folder = game_folder
            self._cat_filter.blockSignals(True)
            self._cat_filter.clear()
            self._cat_filter.addItem("All Categories")
            self._cat_filter.blockSignals(False)
            self._filter.clear()
            self._folder_filter.clear()
            self._tree.clear()
        self._start_search()

    # ------------------------------------------------------------------
    # Profile support
    # ------------------------------------------------------------------

    def load_from_profile(self, profile: dict) -> None:
        """Restore picker state from a profile dict.

        Prefers ``mesh_processed``; falls back to the pre-rename
        ``psk_processed`` key for hand-edited or stale profile dicts.
        """
        processed = profile.get("mesh_processed")
        if processed is None:
            processed = profile.get("psk_processed", [])
        if isinstance(processed, list):
            self._processed_paths = set(str(p) for p in processed)
        else:
            self._processed_paths = set()

        search_dir = profile.get("unpack_output_dir", "")
        if search_dir:
            self.refresh_for_game(search_dir)

        self._checked_paths.clear()

        if self._all_paths:
            self._rebuild_tree()

    def collect_for_profile(self) -> dict:
        """Collect current picker state for saving to a profile."""
        return {
            "mesh_processed": sorted(self._processed_paths),
        }


    def mark_processed(self, paths: list[Path]):
        """Mark paths as processed (blue tint, still selectable)."""
        for p in paths:
            self._processed_paths.add(str(p))
        self._rebuild_tree()

    def unmark_processed(self, paths: list[Path]):
        """Remove processed status from paths so they appear normal again."""
        for p in paths:
            self._processed_paths.discard(str(p))
        self._rebuild_tree()
