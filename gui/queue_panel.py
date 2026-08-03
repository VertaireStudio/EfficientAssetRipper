"""Processing queue panel with progress bar and status tracking."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.asset_scanner import AssetEntry
import gui.theme as theme


def _c(key: str) -> QColor:
    return QColor(theme.current_scheme()[key])


class QueuePanel(QWidget):
    """Displays the processing queue with per-item status and overall progress."""

    process_requested = Signal()        # request to process queued items
    cancel_requested = Signal()
    reprocess_requested = Signal(object)  # AssetEntry to reprocess

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[AssetEntry] = []
        self._batch_offset: int = 0  # row offset for current batch signals
        self._is_resolving: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header with buttons
        header = QHBoxLayout()
        self._header_label = QLabel("Processing Queue")
        header.addWidget(self._header_label)
        header.addStretch()

        self._process_btn = QPushButton("Process Queue")
        self._process_btn.clicked.connect(self.process_requested.emit)
        self._process_btn.setProperty("cssClass", "success")
        header.addWidget(self._process_btn)

        self._clear_btn = QPushButton("Clear Queue")
        self._clear_btn.clicked.connect(self.clear_queue)
        header.addWidget(self._clear_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancel_requested.emit)
        header.addWidget(self._cancel_btn)

        layout.addLayout(header)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setFormat("%v / %m (%p%)")
        layout.addWidget(self._progress)

        # Queue table
        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Asset", "Status", "Details"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        # Status summary
        self._summary = QLabel("")
        layout.addWidget(self._summary)

    def add_to_queue(self, assets: list[AssetEntry]) -> int:
        """Append *assets* to the queue. Returns the row offset of this batch."""
        if not assets:
            return len(self._items)

        # De-duplicate by psk_path against items already in queue
        existing_paths = {str(a.psk_path) for a in self._items}
        new_assets = [a for a in assets if str(a.psk_path) not in existing_paths]
        if not new_assets:
            return len(self._items)

        offset = len(self._items)
        self._items.extend(new_assets)

        old_count = self._table.rowCount()
        self._table.setRowCount(old_count + len(new_assets))

        for i, asset in enumerate(new_assets):
            row = old_count + i
            self._table.setItem(row, 0, QTableWidgetItem(asset.name))
            status_item = QTableWidgetItem("Pending")
            status_item.setForeground(_c("text_secondary"))
            self._table.setItem(row, 1, status_item)
            self._table.setItem(row, 2, QTableWidgetItem(""))

        self._summary.setText(f"{len(self._items)} items in queue")
        return offset

    def get_pending_assets(self) -> list[AssetEntry]:
        """Return queued items whose status column still says 'Pending'."""
        pending: list[AssetEntry] = []
        for row in range(self._table.rowCount()):
            status_item = self._table.item(row, 1)
            if status_item and status_item.text() == "Pending":
                if row < len(self._items):
                    pending.append(self._items[row])
        return pending

    def get_pending_offset(self) -> int:
        """Return the row index of the first pending item (batch offset)."""
        for row in range(self._table.rowCount()):
            status_item = self._table.item(row, 1)
            if status_item and status_item.text() == "Pending":
                return row
        return len(self._items)

    def clear_queue(self):
        """Remove all items from the queue."""
        self._items.clear()
        self._table.setRowCount(0)
        self._progress.setMaximum(1)
        self._progress.setValue(0)
        self._summary.setText("")
        self._batch_offset = 0

    def begin_processing(self, batch_offset: int, batch_size: int):
        """Prepare progress bar for a new processing batch."""
        self._batch_offset = batch_offset
        self._progress.setMaximum(batch_size)
        self._progress.setValue(0)

    def set_processing(self, enabled: bool):
        """Toggle between processing and idle state."""
        self._process_btn.setEnabled(not enabled)
        self._cancel_btn.setEnabled(enabled)

    def set_processing_enabled(self, enabled: bool, tooltip: str = ""):
        """Allow MainWindow to disable Process Queue when Blender is missing.

        Different from ``set_processing()`` (which toggles based on a *running*
        batch). This is for hard-disabling the button when Blender isn't
        installed, with an explanatory tooltip.
        """
        self._process_btn.setEnabled(enabled)
        self._process_btn.setToolTip(tooltip)

    @Slot(int, str)
    def on_job_started(self, index: int, name: str):
        row = self._batch_offset + index
        if row < self._table.rowCount():
            item = self._table.item(row, 1)
            item.setText("Processing...")
            item.setForeground(_c("info"))

    @Slot(int, str, bool)
    def on_job_completed(self, index: int, name: str, success: bool):
        row = self._batch_offset + index
        if row < self._table.rowCount():
            status_item = self._table.item(row, 1)
            if success:
                status_item.setText("Done")
                status_item.setForeground(_c("success"))
            else:
                status_item.setText("Failed")
                status_item.setForeground(_c("error"))
            self._progress.setValue(index + 1)

    @Slot(int, str, str)
    def on_job_progress(self, index: int, name: str, step: str):
        row = self._batch_offset + index
        if row < self._table.rowCount():
            self._table.item(row, 2).setText(step)

    def set_resolving(self, enabled: bool):
        """Disable/enable the Process Queue button while resolving assets."""
        self._process_btn.setEnabled(not enabled)
        self._cancel_btn.setEnabled(enabled)
        self._is_resolving = enabled
        if enabled:
            self._header_label.setText("Resolving Assets")
            self._progress.setProperty("cssClass", "resolving")
            self._progress.setMaximum(0)
            self._progress.setValue(0)
        else:
            self._header_label.setText("Processing Queue")
            self._progress.setProperty("cssClass", "")
            self._progress.setMaximum(1)
            self._progress.setValue(0)

    def update_resolve_progress(self, current: int, total: int, message: str):
        """Update the progress bar during asset resolution (no-op if not resolving)."""
        if not self._is_resolving:
            return
        if total > 0 and self._progress.maximum() != total:
            self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._progress.setFormat(f"Resolving {current}/{total}")

    def on_queue_finished(self, total: int, succeeded: int, failed: int):
        self.set_processing(False)
        self._progress.setMaximum(total if total > 0 else 1)
        self._progress.setValue(total if total > 0 else 0)
        cancelled = total - succeeded - failed
        # Mark remaining pending items in this batch as Cancelled
        for row in range(self._batch_offset, min(self._batch_offset + total, self._table.rowCount())):
            status_item = self._table.item(row, 1)
            if status_item and status_item.text() in ("Pending", "Processing..."):
                status_item.setText("Cancelled")
                status_item.setForeground(_c("warning"))
        parts = [f"{succeeded} succeeded", f"{failed} failed"]
        if cancelled > 0:
            parts.append(f"{cancelled} cancelled")
        self._summary.setText("Batch complete: " + ", ".join(parts))

    def _on_double_click(self, index):
        """Show asset detail dialog when a row is double-clicked."""
        row = index.row()
        if row < 0 or row >= len(self._items):
            return
        from gui.asset_browser import AssetDetailDialog
        asset = self._items[row]
        dlg = AssetDetailDialog(asset, parent=self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.reprocess_requested.connect(self.reprocess_requested.emit)
        dlg.exec()
