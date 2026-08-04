"""Color scheme picker / customizer dialog.

Spun out of ``gui.settings_panel`` so the swatch grid has room to breathe
without a nested ``QScrollArea`` clipping it inside the Settings dialog.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import config
from gui.color_schemes import (
    BUILTIN_SCHEMES,
    SCHEME_KEYS,
    SCHEMES,
    get_scheme,
    list_scheme_names,
    register_custom_scheme,
)
import gui.theme as theme


_BUILTIN = BUILTIN_SCHEMES

# Internal scheme name used while live-previewing edits. Held in the
# SCHEMES dict only for the lifetime of the dialog and never persisted.
_PREVIEW_SCHEME_NAME = "__preview__"


class ColorSchemeDialog(QDialog):
    """Pick a built-in scheme or customize a user scheme's swatches."""

    scheme_changed = Signal(str)  # active scheme name on accept

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Color Scheme")
        self.setMinimumSize(720, 600)

        self._custom_colors: dict[str, str] = {}
        self._swatch_buttons: dict[str, QPushButton] = {}

        # Snapshot the originally-active scheme so Cancel / live-toggle-off
        # can revert cleanly. If the active scheme is custom, we also need
        # a deep copy of its colour dict in case the user edits it and
        # cancels — the public SCHEMES entry would otherwise stay mutated.
        self._original_scheme = (
            config.get("color_scheme") or theme.current_scheme_name()
        )
        original = SCHEMES.get(self._original_scheme)
        self._original_scheme_colors: dict[str, str] | None = (
            dict(original) if original is not None else None
        )
        self._preview_active = False

        outer = QVBoxLayout(self)

        # ── Scheme picker row ─────────────────────────────────────────
        scheme_row = QHBoxLayout()
        scheme_row.addWidget(QLabel("Scheme:"))
        self._scheme_combo = QComboBox()
        self._scheme_combo.addItems(list_scheme_names())
        current = self._original_scheme
        if current in [self._scheme_combo.itemText(i) for i in range(self._scheme_combo.count())]:
            self._scheme_combo.setCurrentText(current)
        self._scheme_combo.currentTextChanged.connect(self._on_scheme_changed)
        scheme_row.addWidget(self._scheme_combo)

        self._new_scheme_btn = QPushButton("New Custom…")
        self._new_scheme_btn.clicked.connect(self._new_custom_scheme)
        scheme_row.addWidget(self._new_scheme_btn)

        self._delete_scheme_btn = QPushButton("Delete Custom")
        self._delete_scheme_btn.clicked.connect(self._delete_custom_scheme)
        scheme_row.addWidget(self._delete_scheme_btn)

        self._reset_btn = QPushButton("Reset to Default")
        self._reset_btn.setToolTip(
            "For a custom scheme, reset every colour to the default scheme's "
            "value. Has no effect on built-in schemes."
        )
        self._reset_btn.clicked.connect(self._reset_to_default)
        scheme_row.addWidget(self._reset_btn)

        self._live_chk = QCheckBox("Apply live")
        self._live_chk.setToolTip(
            "Apply colour changes to the running app as you make them. "
            "Cancel still reverts to the previous scheme."
        )
        self._live_chk.toggled.connect(self._on_live_toggled)
        scheme_row.addWidget(self._live_chk)

        scheme_row.addStretch()
        outer.addLayout(scheme_row)

        # ── Swatch grid (no max-height clamp) ─────────────────────────
        self._color_scroll = QScrollArea()
        self._color_scroll.setWidgetResizable(True)
        self._color_grid_widget = QWidget()
        self._color_grid = QGridLayout(self._color_grid_widget)
        self._color_grid.setContentsMargins(4, 4, 4, 4)
        self._color_grid.setSpacing(4)
        self._color_scroll.setWidget(self._color_grid_widget)
        outer.addWidget(self._color_scroll, stretch=1)

        # ── Buttons ───────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Seed the grid with the currently selected scheme's colors
        self._custom_colors = dict(get_scheme(self._scheme_combo.currentText()))
        self._populate_color_grid()
        self._update_delete_button()
        self._update_reset_button()

    # ------------------------------------------------------------------
    # Selection & grid population
    # ------------------------------------------------------------------

    def _on_scheme_changed(self, name: str):
        self._custom_colors = dict(get_scheme(name))
        self._populate_color_grid()
        self._update_delete_button()
        self._update_reset_button()
        if self._live_chk.isChecked():
            self._apply_preview()

    def _populate_color_grid(self):
        while self._color_grid.count():
            item = self._color_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._swatch_buttons.clear()

        scheme_name = self._scheme_combo.currentText()
        colors = dict(get_scheme(scheme_name))
        if self._custom_colors:
            colors.update(self._custom_colors)
        else:
            self._custom_colors = dict(colors)

        editable = scheme_name not in _BUILTIN

        # Pull the swatch border from the active theme so it stays visible
        # against any background — built-in or user-defined.
        border_hex = theme.current_scheme().get("border_light", "#666666")

        cols = 4
        for idx, key in enumerate(SCHEME_KEYS):
            row, col = divmod(idx, cols)
            container = QHBoxLayout()
            lbl = QLabel(key.replace("_", " ").title())
            lbl.setFixedWidth(120)
            container.addWidget(lbl)

            btn = QPushButton()
            btn.setFixedSize(36, 22)
            hex_color = colors.get(key, "#888888")
            btn.setStyleSheet(
                f"background-color: {hex_color}; border: 1px solid {border_hex}; border-radius: 3px;"
            )
            btn.setToolTip(hex_color)
            if editable:
                btn.clicked.connect(lambda checked=False, k=key: self._pick_color(k))
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                btn.setEnabled(False)
            self._swatch_buttons[key] = btn
            container.addWidget(btn)

            wrapper = QWidget()
            wrapper.setLayout(container)
            self._color_grid.addWidget(wrapper, row, col)

    def _pick_color(self, key: str):
        current = QColor(self._custom_colors.get(key, "#888888"))
        color = QColorDialog.getColor(current, self, f"Pick colour for {key}")
        if color.isValid():
            hex_val = color.name()
            self._custom_colors[key] = hex_val
            btn = self._swatch_buttons.get(key)
            if btn:
                border_hex = theme.current_scheme().get("border_light", "#666666")
                btn.setStyleSheet(
                    f"background-color: {hex_val}; border: 1px solid {border_hex}; border-radius: 3px;"
                )
                btn.setToolTip(hex_val)
            if self._live_chk.isChecked():
                self._apply_preview()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _new_custom_scheme(self):
        name, ok = QInputDialog.getText(self, "New Custom Scheme", "Scheme name:")
        if not ok or not name:
            return
        name = name.strip()
        if name in SCHEMES:
            QMessageBox.warning(self, "Duplicate", f"A scheme named '{name}' already exists.")
            return

        base = dict(get_scheme(self._scheme_combo.currentText()))
        register_custom_scheme(name, base)
        self._save_custom_schemes_to_config()

        self._scheme_combo.blockSignals(True)
        self._scheme_combo.clear()
        self._scheme_combo.addItems(list_scheme_names())
        self._scheme_combo.setCurrentText(name)
        self._scheme_combo.blockSignals(False)

        self._custom_colors = dict(base)
        self._populate_color_grid()
        self._update_delete_button()

    def _delete_custom_scheme(self):
        name = self._scheme_combo.currentText()
        if name in _BUILTIN:
            return
        reply = QMessageBox.question(
            self, "Delete Scheme",
            f"Delete custom scheme '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        SCHEMES.pop(name, None)
        self._save_custom_schemes_to_config()

        self._scheme_combo.blockSignals(True)
        self._scheme_combo.clear()
        self._scheme_combo.addItems(list_scheme_names())
        self._scheme_combo.setCurrentText("Dusk")
        self._scheme_combo.blockSignals(False)

        self._on_scheme_changed("Dusk")

    def _update_delete_button(self):
        name = self._scheme_combo.currentText()
        self._delete_scheme_btn.setEnabled(name not in _BUILTIN)

    def _update_reset_button(self):
        name = self._scheme_combo.currentText()
        # The button only does something for editable (custom) schemes;
        # disable on built-ins so users don't expect an effect that won't
        # come.
        self._reset_btn.setEnabled(name not in _BUILTIN)

    def _reset_to_default(self):
        from gui.color_schemes import DEFAULT_SCHEME
        name = self._scheme_combo.currentText()
        if name in _BUILTIN:
            return
        confirm = QMessageBox.question(
            self, "Reset Scheme",
            f"Reset every colour in '{name}' to the {DEFAULT_SCHEME} defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        defaults = dict(SCHEMES[DEFAULT_SCHEME])
        register_custom_scheme(name, defaults)
        self._custom_colors = dict(defaults)
        self._populate_color_grid()
        self._save_custom_schemes_to_config()
        if self._live_chk.isChecked():
            self._apply_preview()

    def _save_custom_schemes_to_config(self):
        custom = {sname: scolors for sname, scolors in SCHEMES.items()
                  if sname not in _BUILTIN and sname != _PREVIEW_SCHEME_NAME}
        config.set("custom_schemes", json.dumps(custom))

    # ------------------------------------------------------------------
    # Live preview
    # ------------------------------------------------------------------

    def _on_live_toggled(self, checked: bool):
        if checked:
            self._apply_preview()
        else:
            self._revert_preview()

    def _apply_preview(self):
        """Push the currently-selected scheme + edits to the running app."""
        app = QApplication.instance()
        if app is None:
            return
        scheme_name = self._scheme_combo.currentText()
        if scheme_name in _BUILTIN:
            theme.apply(app, scheme_name)
        else:
            # Register under a sentinel so cancelling doesn't leave the real
            # custom-scheme entry mutated.
            register_custom_scheme(_PREVIEW_SCHEME_NAME, dict(self._custom_colors))
            theme.apply(app, _PREVIEW_SCHEME_NAME)
        self._preview_active = True

    def _revert_preview(self):
        """Restore whatever was active before the dialog opened."""
        if not self._preview_active:
            return
        app = QApplication.instance()
        if app is not None:
            # Re-register the original (custom) scheme verbatim in case
            # earlier edits had previewed under the same name and mutated
            # the public dict. Built-ins are immutable so this is a no-op
            # for them.
            if (
                self._original_scheme not in _BUILTIN
                and self._original_scheme_colors is not None
            ):
                register_custom_scheme(
                    self._original_scheme,
                    dict(self._original_scheme_colors),
                )
            theme.apply(app, self._original_scheme)
        SCHEMES.pop(_PREVIEW_SCHEME_NAME, None)
        self._preview_active = False

    # ------------------------------------------------------------------
    # Accept / Reject
    # ------------------------------------------------------------------

    def _accept(self):
        scheme_name = self._scheme_combo.currentText()
        # Preview entry must not leak into persisted state.
        SCHEMES.pop(_PREVIEW_SCHEME_NAME, None)

        config.set("color_scheme", scheme_name)

        if self._custom_colors and scheme_name not in _BUILTIN:
            register_custom_scheme(scheme_name, self._custom_colors)
            self._save_custom_schemes_to_config()

        app = QApplication.instance()
        if app:
            theme.apply(app, scheme_name)

        self.scheme_changed.emit(scheme_name)
        self._preview_active = False
        self.accept()

    def reject(self):
        # Throw away any live-preview changes before falling through to
        # QDialog's default reject.
        self._revert_preview()
        super().reject()
