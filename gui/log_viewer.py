"""Color-coded log viewer widget with filtering."""

from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import gui.theme as theme


def _level_colors():
    c = theme.current_scheme()
    return {
        "info":    QColor(c["text_primary"]),
        "success": QColor(c["success"]),
        "warning": QColor(c["warning"]),
        "error":   QColor(c["error"]),
    }


def _readable_text_on(bg_hex: str) -> str:
    """Pick black or white text for *bg_hex* based on perceived luminance.

    Used by the alert banner so the message stays readable whether the
    active scheme's ``warning`` token is a bright yellow or a dim ochre.
    """
    col = QColor(bg_hex)
    # Rec.709 relative luminance — good enough for picking ink colour.
    lum = (0.2126 * col.red() + 0.7152 * col.green() + 0.0722 * col.blue()) / 255.0
    return "#1a1a1a" if lum > 0.5 else "#f0f0f0"


_MAX_LOG_ENTRIES = 5000


class LogViewer(QWidget):
    """Read-only, color-coded log output panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[tuple[str, str]] = []  # (message, level)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Log"))
        toolbar.addStretch()

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "Info", "Success", "Warning", "Error"])
        self._filter_combo.currentTextChanged.connect(self._apply_filter)
        toolbar.addWidget(QLabel("Filter:"))
        toolbar.addWidget(self._filter_combo)

        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setFixedWidth(60)
        self._copy_btn.clicked.connect(self._copy_log)
        toolbar.addWidget(self._copy_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(60)
        self._save_btn.clicked.connect(self._save_log)
        toolbar.addWidget(self._save_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedWidth(60)
        self._clear_btn.clicked.connect(self.clear)
        toolbar.addWidget(self._clear_btn)

        layout.addLayout(toolbar)

        # Inline alert banner (hidden by default) — used for version-mismatch
        # hints and similar high-signal notifications surfaced near the log.
        self._alert_frame = QFrame()
        self._alert_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._alert_frame.setVisible(False)
        c = theme.current_scheme()
        warn = c["warning"]
        text_on_warn = _readable_text_on(warn)
        self._alert_frame.setStyleSheet(
            f"QFrame {{ background: {warn}; border: 1px solid {warn}; "
            f"border-radius: 4px; padding: 6px; }}"
            f"QLabel {{ color: {text_on_warn}; background: transparent; }}"
            f"QPushButton {{ color: {text_on_warn}; background: rgba(0,0,0,0.08); "
            f"border: 1px solid rgba(0,0,0,0.25); border-radius: 3px; padding: 2px 8px; }}"
            f"QPushButton:hover {{ background: rgba(0,0,0,0.18); }}"
        )
        alert_layout = QHBoxLayout(self._alert_frame)
        alert_layout.setContentsMargins(8, 4, 8, 4)
        self._alert_label = QLabel("")
        self._alert_label.setWordWrap(True)
        self._alert_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        alert_layout.addWidget(self._alert_label, 1)
        self._alert_dismiss = QPushButton("Dismiss")
        self._alert_dismiss.setFixedHeight(22)
        self._alert_dismiss.clicked.connect(self.hide_alert)
        alert_layout.addWidget(self._alert_dismiss, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._alert_frame)

        # Log text area
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Cascadia Code", 9))
        # Bound the on-screen text so an infinite spam stream can't OOM the GUI.
        self._text.document().setMaximumBlockCount(_MAX_LOG_ENTRIES)

        layout.addWidget(self._text)

    @Slot(str, str)
    def append(self, message: str, level: str = "info"):
        """Append a log message with color coding."""
        self._entries.append((message, level))
        # Cap the in-memory backlog too (drop oldest).
        if len(self._entries) > _MAX_LOG_ENTRIES:
            del self._entries[: len(self._entries) - _MAX_LOG_ENTRIES]

        # Check filter
        current_filter = self._filter_combo.currentText().lower()
        if current_filter != "all" and current_filter != level:
            return

        self._append_formatted(message, level)

    def _append_formatted(self, message: str, level: str):
        colors = _level_colors()
        color = colors.get(level, colors["info"])
        fmt = QTextCharFormat()
        fmt.setForeground(color)

        cursor = self._text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(message + "\n", fmt)
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def _apply_filter(self, filter_text: str):
        self._text.clear()
        level_filter = filter_text.lower()
        for message, level in self._entries:
            if level_filter == "all" or level_filter == level:
                self._append_formatted(message, level)

    def clear(self):
        self._entries.clear()
        self._text.clear()

    @Slot(str)
    def show_alert(self, message: str):
        """Display a high-signal banner above the log text. Empty message hides it."""
        if not message:
            self.hide_alert()
            return
        self._alert_label.setText(message)
        self._alert_frame.setVisible(True)

    @Slot()
    def hide_alert(self):
        self._alert_frame.setVisible(False)
        self._alert_label.setText("")

    def _copy_log(self):
        # Build the clipboard payload line-by-line and stop at the entry
        # cap: `_entries` is already bounded to _MAX_LOG_ENTRIES, so this
        # transient string can never exceed _MAX_LOG_ENTRIES lines even
        # mid-loop.
        lines: list[str] = []
        for msg, _level in self._entries:
            if len(lines) >= _MAX_LOG_ENTRIES:
                break
            lines.append(msg)
        QApplication.clipboard().setText("\n".join(lines))

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Log", "batch_log.txt", "Text Files (*.txt)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                for msg, level in self._entries:
                    f.write(f"[{level.upper()}] {msg}\n")
