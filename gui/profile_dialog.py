"""Profile manager dialog — create, rename, delete, and edit game profiles.

Replaces the old inline [New][Rename][Delete] buttons on the profile bar with
a single popup. Edits are committed to disk via OK/Apply; Cancel reverts in-
memory edits without writing back.
"""

from __future__ import annotations

import copy
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.profile_manager import ProfileLoadError, ProfileManager
from gui.theme import install_combo_click_to_popup
from gui.widgets import CollapsibleSection, PathPicker

log = logging.getLogger(__name__)


class AesKeysTableWidget(QWidget):
    """Reusable AES keys editor (label / GUID / hex key) with Add/Remove buttons.

    Used by the profile dialog and the encrypted-archive prompt so both edit
    points share one implementation.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Label", "GUID", "Key (hex)"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._emit_changed)
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        add_btn = QPushButton("Add Key")
        add_btn.clicked.connect(self._add_row)
        btns.addWidget(add_btn)
        rm_btn = QPushButton("Remove Selected")
        rm_btn.clicked.connect(self._remove_selected)
        btns.addWidget(rm_btn)
        btns.addStretch()
        layout.addLayout(btns)

    @property
    def table(self) -> QTableWidget:
        return self._table

    def populate(self, keys: list[dict]):
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(0)
            for k in keys or []:
                row = self._table.rowCount()
                self._table.insertRow(row)
                self._table.setItem(row, 0, QTableWidgetItem(k.get("label", "")))
                self._table.setItem(row, 1, QTableWidgetItem(k.get("guid", "")))
                self._table.setItem(row, 2, QTableWidgetItem(k.get("key", "")))
        finally:
            self._table.blockSignals(False)

    def collect(self) -> list[dict]:
        keys: list[dict] = []
        for row in range(self._table.rowCount()):
            label = (self._table.item(row, 0) or QTableWidgetItem()).text().strip()
            guid = (self._table.item(row, 1) or QTableWidgetItem()).text().strip()
            key = (self._table.item(row, 2) or QTableWidgetItem()).text().strip()
            if key:
                keys.append({"label": label, "guid": guid, "key": key})
        return keys

    def add_prefilled_row(self, label: str = "Main", guid: str = "00000000000000000000000000000000", key: str = ""):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(label))
        self._table.setItem(row, 1, QTableWidgetItem(guid))
        self._table.setItem(row, 2, QTableWidgetItem(key))
        self._emit_changed()

    def _add_row(self):
        self.add_prefilled_row()

    def _remove_selected(self):
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()), reverse=True)
        for row in rows:
            self._table.removeRow(row)
        if rows:
            self._emit_changed()

    def _emit_changed(self, *_):
        self.changed.emit()

# UE versions exposed in the editor (mirrors gui/unpacker_panel._UE_VERSIONS,
# but we keep the editor list short for sanity — users can free-type in the
# unpacker tab if they need an EGame enum value not listed here)
_UE_VERSIONS = [
    "GAME_UE5_9", "GAME_UE5_8", "GAME_UE5_7", "GAME_UE5_6",
    "GAME_UE5_5", "GAME_UE5_4", "GAME_UE5_3", "GAME_UE5_2",
    "GAME_UE5_1", "GAME_UE5_0",
    "GAME_UE4_27", "GAME_UE4_26", "GAME_UE4_25", "GAME_UE4_24",
    "GAME_UE4_23", "GAME_UE4_22", "GAME_UE4_21", "GAME_UE4_20",
    "GAME_RocketLeague",
    "GAME_Valorant",
    "GAME_FortniteBR",
]


class ProfileDialog(QDialog):
    """Modal profile manager.

    The left pane lists all profiles with [New / Rename / Delete] buttons.
    The right pane edits the selected profile's fields. Apply commits the
    currently selected profile; OK commits and closes; Cancel discards any
    pending edits to the currently selected profile.

    On close, ``profile_changed`` fires with the active-profile name so the
    main window can refresh the dropdown and reload state if the user
    renamed/deleted/created the active profile.
    """

    profile_changed = Signal(str)   # current active profile after dialog close
    profile_renamed = Signal(str, str)  # (old, new)
    profile_deleted = Signal(str)
    profile_created = Signal(str)

    def __init__(self, manager: ProfileManager, current_profile: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Profiles")
        self.setMinimumSize(720, 540)

        self._manager = manager
        self._current_profile = current_profile
        # In-memory cache of edits, keyed by profile name. Loaded lazily on
        # first selection. Cancel discards this and re-loads from disk.
        self._cache: dict[str, dict] = {}
        self._dirty: set[str] = set()
        # Tracks profile that was active when the dialog opened so callers
        # can know if it was renamed/deleted.
        self._initial_profile = current_profile

        self._build_ui()
        self._refresh_list(select=current_profile)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left pane: profile list + crud buttons ────────────────────
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_layout.addWidget(QLabel("Profiles"))
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._list, stretch=1)

        crud_row = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._on_new)
        crud_row.addWidget(new_btn)
        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(self._on_rename)
        crud_row.addWidget(rename_btn)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_delete)
        crud_row.addWidget(delete_btn)
        left_layout.addLayout(crud_row)

        splitter.addWidget(left)

        # ── Right pane: editor for selected profile (in a scroll area
        # so collapsed-by-default sections expand without clipping the
        # dialog) ─────────────────────────────────────────────────────
        self._editor = _ProfileEditor(parent=self)
        self._editor.field_changed.connect(self._on_field_changed)
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        editor_scroll.setWidget(self._editor)
        splitter.addWidget(editor_scroll)

        splitter.setSizes([240, 480])
        outer.addWidget(splitter, stretch=1)

        # ── OK / Apply / Cancel ───────────────────────────────────────
        self._btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._btns.accepted.connect(self._on_ok)
        self._btns.rejected.connect(self._on_cancel)
        self._btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        outer.addWidget(self._btns)

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------

    def _refresh_list(self, select: str | None = None):
        self._list.blockSignals(True)
        self._list.clear()
        names = self._manager.list_profiles()
        for name in names:
            item = QListWidgetItem(name)
            self._list.addItem(item)
        if select and select in names:
            for i in range(self._list.count()):
                if self._list.item(i).text() == select:
                    self._list.setCurrentRow(i)
                    break
        elif names:
            self._list.setCurrentRow(0)
        self._list.blockSignals(False)
        # Manually trigger the load for whatever ended up selected
        item = self._list.currentItem()
        self._load_profile_into_editor(item.text() if item else "")

    def _on_selection_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None):
        # Save pending edits from the previous profile into the cache
        if previous is not None:
            self._capture_editor_into_cache(previous.text())
        self._load_profile_into_editor(current.text() if current else "")

    def _load_profile_into_editor(self, name: str):
        if not name:
            self._editor.set_enabled_for_profile(False)
            self._editor.clear()
            self._current_profile = ""
            return

        if name not in self._cache:
            try:
                self._cache[name] = self._manager.load_profile(name)
            except FileNotFoundError:
                # Profile vanished underneath us; re-list and bail
                self._refresh_list()
                return
            except ProfileLoadError as e:
                log.error("Failed to load profile '%s' in dialog: %s", name, e)
                QMessageBox.critical(
                    self, "Profile load failed",
                    f"Could not read profile '{name}'.\n\n{e}",
                )
                self._refresh_list()
                return

        self._current_profile = name
        self._editor.set_enabled_for_profile(True)
        self._editor.load_data(self._cache[name])

    def _capture_editor_into_cache(self, name: str):
        if not name or name not in self._cache:
            return
        new_data = dict(self._cache[name])
        new_data.update(self._editor.collect_data())
        if new_data != self._cache[name]:
            self._cache[name] = new_data
            self._dirty.add(name)

    def _on_field_changed(self):
        # Mark current profile dirty as soon as any field changes
        if self._current_profile:
            self._dirty.add(self._current_profile)

    # ------------------------------------------------------------------
    # CRUD actions
    # ------------------------------------------------------------------

    def _on_new(self):
        # Capture pending edits to current profile first so they aren't lost
        self._capture_editor_into_cache(self._current_profile)

        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok or not name:
            return
        valid, reason = ProfileManager.is_valid_name(name)
        if not valid:
            QMessageBox.warning(self, "Invalid Name", reason)
            return
        if self._manager.profile_exists(name):
            QMessageBox.warning(self, "Duplicate", f"A profile named '{name}' already exists.")
            return

        # Create on disk immediately so list_profiles() picks it up; the user
        # can fill in fields and Apply to commit.
        self._manager.create_profile(name)
        self._cache[name] = self._manager.load_profile(name)
        self._refresh_list(select=name)
        self.profile_created.emit(name)

    def _on_rename(self):
        item = self._list.currentItem()
        if not item:
            return
        old = item.text()

        # Capture pending edits before rename
        self._capture_editor_into_cache(old)

        new, ok = QInputDialog.getText(self, "Rename Profile", "New name:", text=old)
        if not ok or not new or new == old:
            return
        valid, reason = ProfileManager.is_valid_name(new)
        if not valid:
            QMessageBox.warning(self, "Invalid Name", reason)
            return
        if self._manager.profile_exists(new):
            QMessageBox.warning(self, "Duplicate", f"A profile named '{new}' already exists.")
            return

        # Persist any pending edits first so the rename carries them
        if old in self._dirty and old in self._cache:
            try:
                self._manager.save_profile(old, self._cache[old])
                self._dirty.discard(old)
            except Exception:
                log.exception("save before rename failed for %s", old)

        try:
            self._manager.rename_profile(old, new)
        except Exception as e:
            QMessageBox.critical(self, "Rename failed", str(e))
            return

        # Update cache key
        if old in self._cache:
            self._cache[new] = self._cache.pop(old)
        if old in self._dirty:
            self._dirty.discard(old)

        if self._initial_profile == old:
            self._initial_profile = new

        self._refresh_list(select=new)
        self.profile_renamed.emit(old, new)

    def _on_delete(self):
        item = self._list.currentItem()
        if not item:
            return
        name = item.text()

        if len(self._manager.list_profiles()) <= 1:
            QMessageBox.information(self, "Cannot Delete", "You must have at least one profile.")
            return

        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Delete profile '{name}'?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self._manager.delete_profile(name)
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))
            return

        self._cache.pop(name, None)
        self._dirty.discard(name)
        self._refresh_list()
        self.profile_deleted.emit(name)

    # ------------------------------------------------------------------
    # OK / Apply / Cancel
    # ------------------------------------------------------------------

    def _commit_dirty(self) -> bool:
        """Persist all dirty profiles to disk. Returns True on success."""
        # Capture in-flight edits to the currently shown profile
        self._capture_editor_into_cache(self._current_profile)

        for name in list(self._dirty):
            data = self._cache.get(name)
            if data is None:
                continue
            try:
                self._manager.save_profile(name, data)
            except Exception as e:
                QMessageBox.critical(self, "Save failed", f"{name}: {e}")
                return False
        self._dirty.clear()
        return True

    def _on_ok(self):
        if not self._commit_dirty():
            return
        self.profile_changed.emit(self._current_profile)
        self.accept()

    def _on_apply(self):
        if not self._commit_dirty():
            return
        self.profile_changed.emit(self._current_profile)

    def _on_cancel(self):
        # Discard in-memory edits; on-disk state is unchanged unless New/Rename/
        # Delete was used (those always commit immediately to keep the list view
        # in sync with reality).
        self.reject()


# ---------------------------------------------------------------------------
# Per-profile editor pane
# ---------------------------------------------------------------------------

class _ProfileEditor(QWidget):
    """Editor for a single profile's fields."""

    field_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._building = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 0, 0, 0)

        # ── Paths ─────────────────────────────────────────────────────
        paths_section = CollapsibleSection("Paths", start_expanded=False)
        paths_form = QFormLayout()

        self._game_dir = PathPicker(mode="folder", title="Select Game Folder")
        self._game_dir.setPlaceholderText("Path to game .pak/.utoc folder (or loose-content root)")
        self._game_dir.changed.connect(self._on_changed)
        paths_form.addRow("Game folder:", self._game_dir)

        self._mounted_dir = PathPicker(mode="folder", title="Select Mounted Folder")
        self._mounted_dir.setPlaceholderText("Where mounted/exported files land — also where PSK Picker reads from")
        self._mounted_dir.changed.connect(self._on_changed)
        paths_form.addRow("Mounted folder:", self._mounted_dir)

        self._output_dir = PathPicker(mode="folder", title="Select Output Folder")
        self._output_dir.setPlaceholderText("Where Blender writes .blend output files")
        self._output_dir.changed.connect(self._on_changed)
        paths_form.addRow("Output folder:", self._output_dir)

        paths_section.set_content_layout(paths_form)
        outer.addWidget(paths_section)

        # ── Mount config ──────────────────────────────────────────────
        mount_section = CollapsibleSection("Mount", start_expanded=False)
        mount_form = QFormLayout()

        self._ue_combo = QComboBox()
        self._ue_combo.setEditable(True)
        self._ue_combo.addItems(_UE_VERSIONS)
        install_combo_click_to_popup(self._ue_combo)
        self._ue_combo.currentTextChanged.connect(self._on_changed)
        mount_form.addRow("UE Version:", self._ue_combo)

        self._mappings = PathPicker(mode="file", filter_str="USMAP Files (*.usmap);;All Files (*)",
                                     title="Select Mappings File")
        self._mappings.setPlaceholderText("Optional .usmap file")
        self._mappings.changed.connect(self._on_changed)
        mount_form.addRow("Mappings:", self._mappings)

        self._auto_save_chk = QCheckBox(
            "Auto-save Unpacker tab edits to this profile"
        )
        self._auto_save_chk.setToolTip(
            "When checked, edits to Game folder / UE version / Mounted folder in the\n"
            "Unpacker tab are written back to this profile. When unchecked, those fields\n"
            "stay editable for one-off mounts but never overwrite the saved profile values."
        )
        self._auto_save_chk.toggled.connect(self._on_changed)
        mount_form.addRow("", self._auto_save_chk)

        mount_section.set_content_layout(mount_form)
        outer.addWidget(mount_section)

        # ── AES keys ──────────────────────────────────────────────────
        keys_section = CollapsibleSection("AES Keys", start_expanded=False)
        keys_layout = QVBoxLayout()

        self._keys_table = AesKeysTableWidget()
        self._keys_table.changed.connect(self._on_changed)
        keys_layout.addWidget(self._keys_table)

        keys_section.set_content_layout(keys_layout)
        outer.addWidget(keys_section)

        # ── Texture Resolution ────────────────────────────────────────
        # Per-game preferences for material wiring: which preset is used by
        # default, whether the keyword auto-detect fallback runs, and the
        # number of per-material overrides currently saved.
        tex_section = CollapsibleSection("Texture Resolution", start_expanded=False)
        tex_form = QFormLayout()

        self._texture_preset = QComboBox()
        self._texture_preset.setToolTip(
            "The preset used to wire materials for this game's assets.\n"
            "default_pbr handles standard UE4/5 layouts; simple_diffuse and\n"
            "nrm_packed are alternatives. Per-material overrides below win\n"
            "regardless of this default."
        )
        # Populate from presets JSON; tolerate any failure here (config.load_presets
        # already falls back to bundled defaults silently).
        try:
            import config as _cfg
            preset_names = list(_cfg.load_presets().get("presets", {}).keys())
        except Exception:
            preset_names = ["default_pbr"]
        self._texture_preset.addItems(preset_names or ["default_pbr"])
        install_combo_click_to_popup(self._texture_preset)
        self._texture_preset.currentTextChanged.connect(self._on_changed)
        tex_form.addRow("Default preset:", self._texture_preset)

        self._auto_resolve_chk = QCheckBox(
            "Enable keyword auto-detect fallback"
        )
        self._auto_resolve_chk.setToolTip(
            "When the chosen preset's suffix rules can't classify a texture,\n"
            "scan the mesh's nearest Textures/Materials folder and try to fill\n"
            "empty slots by name keywords (basecolor/diffuse/albedo/normal/etc).\n"
            "Helps games whose texture filenames don't follow UE conventions."
        )
        self._auto_resolve_chk.toggled.connect(self._on_changed)
        tex_form.addRow("", self._auto_resolve_chk)

        overrides_row = QHBoxLayout()
        self._overrides_label = QLabel("0 overrides defined")
        overrides_row.addWidget(self._overrides_label)
        overrides_row.addStretch()
        self._edit_overrides_btn = QPushButton("Edit per-material overrides…")
        self._edit_overrides_btn.setToolTip(
            "Open the override editor to force specific textures into specific\n"
            "slots for individual materials in this profile."
        )
        self._edit_overrides_btn.clicked.connect(self._on_edit_overrides_clicked)
        overrides_row.addWidget(self._edit_overrides_btn)
        # Wrap the row in a QWidget so QFormLayout accepts it as a single field.
        overrides_widget = QWidget()
        overrides_widget.setLayout(overrides_row)
        tex_form.addRow("Per-material:", overrides_widget)

        tex_section.set_content_layout(tex_form)
        outer.addWidget(tex_section)
        outer.addStretch()

        # In-memory cache of material_overrides for the active profile —
        # the dialog never round-trips this through GUI widgets, only via
        # the override editor sub-dialog.
        self._material_overrides: dict = {}

        self._building = False

    def _on_changed(self, *_):
        if self._building:
            return
        self.field_changed.emit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_enabled_for_profile(self, enabled: bool):
        for w in (self._game_dir, self._mounted_dir, self._output_dir,
                  self._ue_combo, self._mappings, self._auto_save_chk,
                  self._keys_table, self._texture_preset,
                  self._auto_resolve_chk, self._edit_overrides_btn):
            w.setEnabled(enabled)

    def clear(self):
        self.load_data({})

    def load_data(self, data: dict):
        self._building = True
        try:
            self._game_dir.setText(data.get("game_dir", ""))
            self._mounted_dir.setText(data.get("unpack_output_dir", ""))
            self._output_dir.setText(data.get("blender_output_dir", ""))
            ue = data.get("ue_version", "GAME_UE5_4")
            idx = self._ue_combo.findText(ue)
            if idx >= 0:
                self._ue_combo.setCurrentIndex(idx)
            else:
                # Allow free-typed values not in the dropdown
                self._ue_combo.setEditText(ue)
            self._mappings.setText(data.get("mappings_path", ""))
            self._auto_save_chk.setChecked(bool(data.get("auto_save_paths", False)))

            self._keys_table.populate(data.get("aes_keys", []) or [])

            # Texture-resolution fields. Coerce the preset combo to the saved
            # value, falling back to the default if the saved one is no longer
            # available in the loaded presets JSON.
            saved_preset = data.get("texture_preset", "default_pbr") or "default_pbr"
            idx = self._texture_preset.findText(saved_preset)
            if idx >= 0:
                self._texture_preset.setCurrentIndex(idx)
            else:
                # The preset disappeared from the JSON since this profile was
                # saved — fall back to default_pbr without overwriting the
                # profile's stored value.
                idx = self._texture_preset.findText("default_pbr")
                if idx >= 0:
                    self._texture_preset.setCurrentIndex(idx)
            self._auto_resolve_chk.setChecked(
                bool(data.get("auto_resolve_fallback", True))
            )
            mo = data.get("material_overrides") or {}
            self._material_overrides = dict(mo) if isinstance(mo, dict) else {}
            self._refresh_overrides_label()
        finally:
            self._building = False

    def collect_data(self) -> dict:
        keys = self._keys_table.collect()

        return {
            "game_dir": self._game_dir.text().strip(),
            "unpack_output_dir": self._mounted_dir.text().strip(),
            "blender_output_dir": self._output_dir.text().strip(),
            "ue_version": self._ue_combo.currentText().strip() or "GAME_UE5_4",
            "mappings_path": self._mappings.text().strip(),
            "auto_save_paths": self._auto_save_chk.isChecked(),
            "aes_keys": keys,
            "texture_preset": self._texture_preset.currentText().strip() or "default_pbr",
            "auto_resolve_fallback": self._auto_resolve_chk.isChecked(),
            "material_overrides": copy.deepcopy(self._material_overrides),
        }

    def _refresh_overrides_label(self):
        n = len(self._material_overrides)
        self._overrides_label.setText(
            "0 overrides defined" if n == 0 else f"{n} override{'s' if n != 1 else ''} defined"
        )

    def _on_edit_overrides_clicked(self):
        """Open the per-material override editor against the in-memory dict.

        The override editor mutates a copy and returns the updated dict so
        the dialog's standard Apply/Cancel flow still controls persistence.
        """
        from gui.material_overrides_dialog import MaterialOverridesDialog

        try:
            import config as _cfg
            presets_data = _cfg.load_presets()
        except Exception:
            presets_data = {"presets": {"default_pbr": {"texture_slots": {}}}}

        dlg = MaterialOverridesDialog(
            overrides=copy.deepcopy(self._material_overrides),
            presets_data=presets_data,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._material_overrides = dlg.result_overrides()
            self._refresh_overrides_label()
            self._on_changed()

