"""Shared GUI widgets used across multiple panels."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QModelIndex, Signal
from PySide6.QtGui import QAction, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


LAZY_PLACEHOLDER = "__placeholder__"


class _WheelGatedSlider(QSlider):
    """QSlider that defers unfocused mouse-wheel input to its parent.

    A slider inside a ``QScrollArea`` (e.g. the settings dialog) otherwise
    swallows the wheel while the pointer merely hovers over it and silently
    changes its own value. Ignoring the event while unfocused lets it bubble
    up the parent chain so the scroll area keeps scrolling naturally; the
    slider still responds to the wheel once focused (click or Tab to it).
    """

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


class LabeledSlider(QWidget):
    """Horizontal slider with a live value label — a QSpinBox replacement
    for bounded settings (percentages, quality, scale).

    The public surface mirrors QSpinBox's numeric API (``value()``
    ``setValue()`` ``minimum()`` ``maximum()`` ``setRange()``
    ``setSingleStep()`` ``setSuffix()`` ``setSpecialValueText()``
    ``specialValueText()`` plus a ``valueChanged`` signal) so call sites
    written against a spin box keep working unchanged.

    The slider indexes tick positions (``0..N-1``) internally and maps them
    to real values, which makes non-1 steps (e.g. UI scale snapping to
    10 %) behave consistently while dragging — QSlider's own single-step
    setting does not affect drag movement.

    Memory-leak note: both children are owned through the layout/parent
    chain, so nothing external holds them and Qt tears the whole subtree
    down (including this widget's internal signal wiring) when the dialog
    is destroyed. No timers, no event filters on foreign objects.
    """

    valueChanged = Signal(int)

    def __init__(
        self,
        minimum: int,
        maximum: int,
        *,
        value: int | None = None,
        step: int = 1,
        suffix: str = "",
        special_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        if int(maximum) < int(minimum):
            raise ValueError("maximum must be >= minimum")

        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._step = max(1, int(step))
        self._suffix = suffix
        self._special_text = special_text
        self._value = self._minimum

        self._slider = _WheelGatedSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, self._tick_count() - 1)
        self._slider.setSingleStep(1)  # keyboard/wheel step = one tick

        self._label = QLabel()
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Scoped so the theme can gray the caption out when the slider is
        # disabled (styling a plain QLabel globally would be too broad).
        self._label.setProperty("cssClass", "slider-value")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._label)

        # Refresh the label before wiring the signal so construction never
        # emits to handlers the caller hasn't connected yet.
        self._update_label_width()
        self._refresh_label()

        self._slider.valueChanged.connect(self._on_tick_changed)
        if value is not None:
            self.setValue(value)

    # -- Mapping between slider tick positions and real values ------------

    def _tick_count(self) -> int:
        """Number of representable stops on the slider groove."""
        span = self._maximum - self._minimum
        count = span // self._step + 1
        if span % self._step:
            # Non-divisible span: one extra stop pins the far end of the
            # groove to ``maximum`` (all other stops stay on the step grid).
            count += 1
        return count

    def _tick_to_value(self, tick: int) -> int:
        last = self._tick_count() - 1
        if tick >= last:
            return self._maximum
        return self._minimum + tick * self._step

    def _value_to_tick(self, value: int) -> int:
        value = max(self._minimum, min(int(value), self._maximum))
        offset = value - self._minimum
        last = self._tick_count() - 1
        if (self._maximum - self._minimum) % self._step:
            # Above the top regular step → clamp onto the pinned end stop.
            if offset > (last - 1) * self._step:
                return last
        # Integer half-up rounding onto the step grid.
        tick = (offset * 2 + self._step) // (2 * self._step)
        return max(0, min(tick, last))

    # -- Internals ---------------------------------------------------------

    def _refresh_label(self) -> None:
        self._label.setText(self._fmt_value(self._value))

    def _fmt_value(self, value: int) -> str:
        if self._special_text and value == self._minimum:
            return self._special_text
        return f"{value}{self._suffix}"

    def _update_label_width(self) -> None:
        """Reserve the widest expected caption so the label never jitters
        while the slider is being dragged (double-digit ↔ triple-digit)."""
        fm = self._label.fontMetrics()
        candidates = [
            f"{self._maximum}{self._suffix}",
            f"{self._minimum}{self._suffix}",
        ]
        if self._special_text:
            candidates.append(self._special_text)
        self._label.setMinimumWidth(
            max(fm.horizontalAdvance(text) for text in candidates) + 4
        )

    def _set_value_internal(self, value: int) -> None:
        """Assign ``self._value``, refresh the caption, notify listeners.

        QSpinBox parity: setting the same value again does not emit.
        """
        if value == self._value:
            return
        self._value = value
        self._refresh_label()
        self.valueChanged.emit(value)

    def _on_tick_changed(self, tick: int) -> None:
        self._set_value_internal(self._tick_to_value(tick))

    def _rebuild_ticks(self) -> None:
        """Re-derive the groove after range/step changes, keeping the
        current value (clamped into the new bounds)."""
        self._slider.blockSignals(True)
        self._slider.setRange(0, self._tick_count() - 1)
        self._slider.blockSignals(False)
        self.setValue(self._value)

    # -- QSpinBox-compatible public API ------------------------------------

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        value = max(self._minimum, min(int(value), self._maximum))
        tick = self._value_to_tick(value)
        if tick != self._slider.value():
            # Drive the slider silently; the mapped value below performs the
            # emitting so there's exactly one signal per change even when
            # the snapped tick differs from the requested value.
            self._slider.blockSignals(True)
            self._slider.setValue(tick)
            self._slider.blockSignals(False)
        # Store the value the slider geometry actually represents — a step
        # grid may snap the requested value onto a neighbouring stop.
        self._set_value_internal(self._tick_to_value(tick))

    def setRange(self, minimum: int, maximum: int, step: int | None = None):
        if int(maximum) < int(minimum):
            raise ValueError("maximum must be >= minimum")
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        if step is not None:
            self._step = max(1, int(step))
        self._rebuild_ticks()
        self._update_label_width()
        self._refresh_label()

    def setSingleStep(self, step: int) -> None:
        step = max(1, int(step))
        if step == self._step:
            return
        self._step = step
        self._rebuild_ticks()

    def setSuffix(self, suffix: str) -> None:
        self._suffix = suffix
        self._update_label_width()
        self._refresh_label()

    def setSpecialValueText(self, text: str) -> None:
        self._special_text = text
        self._update_label_width()
        self._refresh_label()

    def specialValueText(self) -> str:
        return self._special_text

    # -- QWidget overrides --------------------------------------------------

    def setToolTip(self, text: str) -> None:  # type: ignore[override]
        # QWidget.setToolTip is non-virtual on the C++ side, but PySide6
        # resolves the Python override for direct calls — always route the
        # description onto the slider too, since a child widget shows only
        # its own tooltip while hovered, never the parent's.
        super().setToolTip(text)
        self._slider.setToolTip(text)

    @property
    def label_text(self) -> str:
        """Current caption text (tests observe this instead of privates)."""
        return self._label.text()


class PathPicker(QWidget):
    """A line-edit + Browse button for picking files or folders.

    Emits ``changed`` whenever the line edit text changes (Browse and manual
    typing both fire it). ``mode='folder'`` shows a directory dialog;
    ``mode='file'`` shows a file dialog with the optional filter string.
    """

    changed = Signal(str)

    def __init__(
        self,
        mode: str = "folder",
        filter_str: str = "",
        title: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._mode = mode
        self._filter = filter_str
        self._title = title or ("Select Folder" if mode == "folder" else "Select File")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.line_edit = QLineEdit()
        self.line_edit.textChanged.connect(self.changed.emit)
        layout.addWidget(self.line_edit)

        btn = QPushButton("Browse...")
        # No fixed width — a 80px clamp clipped the label ("Browse...") once
        # QSS padding + user font scale outgrew it. Size to content instead.
        btn.clicked.connect(self._browse)
        layout.addWidget(btn)

    def _browse(self):
        start = self.line_edit.text().strip()
        if self._mode == "folder":
            path = QFileDialog.getExistingDirectory(self, self._title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, self._title, start, self._filter)
        if path:
            self.line_edit.setText(path)

    def text(self) -> str:
        return self.line_edit.text()

    def setText(self, text: str):
        self.line_edit.setText(text)

    def setPlaceholderText(self, text: str):
        self.line_edit.setPlaceholderText(text)


class ZoomableTree(QTreeWidget):
    """QTreeWidget that supports Ctrl+Scroll zoom, Shift+Click range checkbox toggling, and Alt+Click recursive expand/collapse."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_font_size = self.font().pointSize()
        if self._base_font_size <= 0:
            self._base_font_size = 10
        self._last_clicked_item: QTreeWidgetItem | None = None
        self._lazy_load_callback: Callable[[QTreeWidgetItem], None] | None = None

    def set_lazy_load_callback(self, cb: Callable[[QTreeWidgetItem], None] | None) -> None:
        """Wire a callback so Alt+click on a folder with placeholder children
        triggers a single browse() rather than infinite recursion."""
        self._lazy_load_callback = cb

    # -- Shift+Click range checkbox toggling + Alt+Click recursive toggle --

    def mousePressEvent(self, event: QMouseEvent):
        item = self.itemAt(event.pos())
        mods = event.modifiers()
        is_left = event.button() == Qt.MouseButton.LeftButton
        is_shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        is_alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        if (
            item is not None
            and is_left
            and is_shift
            and bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            and self._last_clicked_item is not None
            and self._last_clicked_item is not item
        ):
            # Determine target state from the clicked item (toggle it)
            new_state = (
                Qt.CheckState.Unchecked
                if item.checkState(0) == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
            items_in_range = self._get_visible_items_between(self._last_clicked_item, item)
            self.blockSignals(True)
            for it in items_in_range:
                if bool(it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                    it.setCheckState(0, new_state)
            self.blockSignals(False)
            # Emit itemChanged for the range so trackers update
            for it in items_in_range:
                if bool(it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                    self.itemChanged.emit(it, 0)
            self._last_clicked_item = item
            event.accept()
            return

        if (
            item is not None
            and is_left
            and is_alt
            and not is_shift
            and self._is_expandable(item)
        ):
            self.setCurrentItem(item)
            if item.isExpanded():
                self.collapse_recursive(item)
            else:
                self.expand_recursive(item)
            self._last_clicked_item = item
            event.accept()
            return

        if item is not None:
            self._last_clicked_item = item
        super().mousePressEvent(event)

    @staticmethod
    def _is_expandable(item: QTreeWidgetItem) -> bool:
        return item.childCount() > 0

    @staticmethod
    def _is_lazy_placeholder(item: QTreeWidgetItem) -> bool:
        if item.childCount() != 1:
            return False
        child = item.child(0)
        return child.data(0, Qt.ItemDataRole.UserRole) == LAZY_PLACEHOLDER

    def expand_recursive(self, item: QTreeWidgetItem) -> None:
        """Expand item and all descendants. Stops at unloaded lazy boundaries
        (a single placeholder triggers one browse() and returns)."""
        if not self._is_expandable(item):
            return
        if self._is_lazy_placeholder(item):
            item.setExpanded(True)
            cb = self._lazy_load_callback
            if cb is not None:
                cb(item)
            return
        item.setExpanded(True)
        for i in range(item.childCount()):
            child = item.child(i)
            if self._is_expandable(child):
                self.expand_recursive(child)

    def collapse_recursive(self, item: QTreeWidgetItem) -> None:
        """Collapse item and all descendants."""
        for i in range(item.childCount()):
            child = item.child(i)
            if child.childCount() > 0:
                self.collapse_recursive(child)
        item.setExpanded(False)

    def expand_all_visible(self) -> None:
        """Expand every non-hidden item; skips filtered-out rows."""
        root = self.invisibleRootItem()
        for i in range(root.childCount()):
            self._expand_visible_walk(root.child(i))

    def _expand_visible_walk(self, item: QTreeWidgetItem) -> None:
        if item.isHidden():
            return
        if item.childCount() > 0 and not self._is_lazy_placeholder(item):
            item.setExpanded(True)
            for i in range(item.childCount()):
                self._expand_visible_walk(item.child(i))
        elif self._is_lazy_placeholder(item):
            item.setExpanded(True)

    def collapse_all_visible(self) -> None:
        root = self.invisibleRootItem()
        for i in range(root.childCount()):
            self._collapse_visible_walk(root.child(i))

    def _collapse_visible_walk(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            self._collapse_visible_walk(item.child(i))
        item.setExpanded(False)

    def expand_selected(self, recursive: bool = True) -> None:
        for it in self.selectedItems():
            if recursive:
                self.expand_recursive(it)
            else:
                it.setExpanded(True)

    def collapse_selected(self, recursive: bool = True) -> None:
        for it in self.selectedItems():
            if recursive:
                self.collapse_recursive(it)
            else:
                it.setExpanded(False)

    def _get_visible_items_between(
        self, item_a: QTreeWidgetItem, item_b: QTreeWidgetItem
    ) -> list[QTreeWidgetItem]:
        """Return all visible items between item_a and item_b (inclusive)."""
        all_visible = self._collect_visible_items()
        try:
            idx_a = all_visible.index(item_a)
            idx_b = all_visible.index(item_b)
        except ValueError:
            return [item_b]
        lo, hi = min(idx_a, idx_b), max(idx_a, idx_b)
        return all_visible[lo : hi + 1]

    def _collect_visible_items(self) -> list[QTreeWidgetItem]:
        """Collect all currently visible (expanded) items in display order."""
        result: list[QTreeWidgetItem] = []
        root = self.invisibleRootItem()
        self._walk_visible(root, result)
        return result

    def _walk_visible(self, parent: QTreeWidgetItem, out: list[QTreeWidgetItem]):
        for i in range(parent.childCount()):
            child = parent.child(i)
            out.append(child)
            if child.isExpanded() and child.childCount() > 0:
                self._walk_visible(child, out)

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            font = self.font()
            size = font.pointSize()
            if size <= 0:
                size = 10
            if delta > 0:
                new_size = min(size + 1, 48)
            elif delta < 0:
                new_size = max(size - 1, 6)
            else:
                new_size = size
            if new_size != size:
                # Scale column widths in proportion to the font size so the
                # zoom level doesn't crush narrow columns into illegibility.
                ratio = new_size / size
                for col in range(self.columnCount()):
                    self.setColumnWidth(col, max(20, int(self.columnWidth(col) * ratio)))
                font.setPointSize(new_size)
                self.setFont(font)
            event.accept()
        else:
            super().wheelEvent(event)


def add_tree_expand_actions(menu: QMenu, tree: ZoomableTree, item: QTreeWidgetItem | None) -> None:
    """Append Expand/Collapse All + Selected entries to a context menu.

    Used by all three left-tab trees. Selected variants are only enabled when
    the right-clicked item itself is expandable.
    """
    has_selection = item is not None and item.childCount() > 0

    if menu.actions():
        menu.addSeparator()

    expand_all = QAction("Expand All", menu)
    expand_all.triggered.connect(tree.expand_all_visible)
    menu.addAction(expand_all)

    collapse_all = QAction("Collapse All", menu)
    collapse_all.triggered.connect(tree.collapse_all_visible)
    menu.addAction(collapse_all)

    expand_selected = QAction("Expand Selected", menu)
    expand_selected.setEnabled(has_selection)
    expand_selected.triggered.connect(lambda: tree.expand_selected(recursive=True))
    menu.addAction(expand_selected)

    collapse_selected = QAction("Collapse Selected", menu)
    collapse_selected.setEnabled(has_selection)
    collapse_selected.triggered.connect(lambda: tree.collapse_selected(recursive=True))
    menu.addAction(collapse_selected)


class CollapsibleSection(QWidget):
    """A collapsible group with a toggle button showing \u25bc/\u25b6 arrows."""

    def __init__(self, title: str, start_expanded: bool = True, closeable: bool = True, parent=None):
        super().__init__(parent)
        self._expanded = start_expanded
        self._title = title
        self._closeable = closeable

        self._toggle_btn = QPushButton(self._arrow() + "  " + title)
        self._toggle_btn.setProperty("cssClass", "collapsible")
        self._toggle_btn.clicked.connect(self._toggle)

        self._content = QWidget()
        self._content.setVisible(self._expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toggle_btn)
        layout.addWidget(self._content)

        # If not closeable and already expanded, hide the toggle button
        if not self._closeable and self._expanded:
            self._toggle_btn.setVisible(False)

    def set_content_layout(self, content_layout):
        self._content.setLayout(content_layout)

    def _arrow(self) -> str:
        return "\u25bc" if self._expanded else "\u25b6"

    def _toggle(self):
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self._toggle_btn.setText(self._arrow() + "  " + self._title)
        if not self._closeable and self._expanded:
            self._toggle_btn.setVisible(False)
