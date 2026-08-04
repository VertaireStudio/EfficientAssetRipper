"""Unpacker panel — browse and extract Unreal Engine game archives."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import CollapsibleSection, ZoomableTree, add_tree_expand_actions

import config
from core import type_cache as type_cache_mod
from core.type_cache import TypeCache
from core.unpacker import UnpackerProcess

log = logging.getLogger(__name__)

# UE versions exposed in the dropdown — grouped by era, newest first
_UE_VERSIONS = [
    # UE5
    "GAME_UE5_9", "GAME_UE5_8", "GAME_UE5_7", "GAME_UE5_6",
    "GAME_UE5_5", "GAME_UE5_4", "GAME_UE5_3", "GAME_UE5_2",
    "GAME_UE5_1", "GAME_UE5_0",
    # UE4 (all minor versions)
    "GAME_UE4_27", "GAME_UE4_26", "GAME_UE4_25", "GAME_UE4_24",
    "GAME_UE4_23", "GAME_UE4_22", "GAME_UE4_21", "GAME_UE4_20",
    "GAME_UE4_19", "GAME_UE4_18", "GAME_UE4_17", "GAME_UE4_16",
    "GAME_UE4_15", "GAME_UE4_14", "GAME_UE4_13", "GAME_UE4_12",
    "GAME_UE4_11", "GAME_UE4_10", "GAME_UE4_9", "GAME_UE4_8",
    "GAME_UE4_7", "GAME_UE4_6", "GAME_UE4_5", "GAME_UE4_4",
    "GAME_UE4_3", "GAME_UE4_2", "GAME_UE4_1", "GAME_UE4_0",
    # Game-specific overrides (CUE4Parse EGame enum)
    "GAME_RocketLeague",
    "GAME_Valorant",
    "GAME_FortniteBR",
    "GAME_Fortnite",
    "GAME_PUBG",
    "GAME_Splitgate",
    "GAME_SeaOfThieves",
    "GAME_GearsOfWar4",
    "GAME_StateOfDecay2",
    "GAME_ArkSurvivalEvolved",
]

_PLACEHOLDER = "__placeholder__"


class UnpackerPanel(QWidget):
    """Left-tab panel for mounting UE archives and exporting assets."""

    psk_extracted = Signal(list)        # list[Path] of extracted PSK/PSKX files
    log_message = Signal(str, str)      # message, level
    props_viewed = Signal(str, str)     # (title, json_text) for Text Viewer
    media_preview = Signal(str)         # local file path for Media Preview tab (audio or video — auto-routed by extension)
    tga_preview = Signal(str)           # local file path for TGA/image Preview tab
    mesh_preview = Signal(str)          # local .psk path for Mesh Preview tab
    version_mismatch = Signal(str)      # banner text for the log viewer
    aes_keys_required = Signal(int, list)   # (unmounted_count, [{"name", "guid"}, ...]) — host opens prompt dialog

    def __init__(self, parent=None):
        super().__init__(parent)
        self._unpacker = UnpackerProcess(self)
        self._mounted = False
        self._exporting = False
        self._export_output_dir = ""
        self._suppress_export_error_popup = False  # user opted to skip popups for the current CLI session
        # Snapshot of AES keys owned by the active profile. The Unpacker no
        # longer has its own editor — these arrive via load_from_profile and
        # are mutated only by the AES prompt (which writes back via the
        # ProfileManager and reissues load_from_profile).
        self._aes_keys: list[dict] = []
        # Single-shot guard so a remount triggered by the AES prompt itself
        # doesn't reopen the prompt if the new keys are still wrong.
        self._aes_prompt_pending = False

        # Hand-off tracking. ``_pre_export_psks`` snapshots ``path → mtime``
        # for every PSK/PSKX in the output directory at export-start. After
        # export, any PSK whose path is NEW or whose mtime advanced is treated
        # as "this session wrote it" — both first-time exports and re-exports
        # over an existing file are picked up. ``_session_handoff_psks``
        # accumulates those across multiple back-to-back exports until the
        # user clicks "Send to Queue". Replaces the older
        # rglob-everything-in-output-dir behavior, which silently scooped up
        # every PSK from prior sessions every time.
        self._pre_export_psks: dict[Path, float] = {}
        self._session_handoff_psks: list[Path] = []

        # WWise audio data (populated by scan after mount)
        self._wwise_root = ""          # e.g. "Game/Content/WwiseAudio/"
        self._wwise_events_prefix = "" # e.g. "Game/Content/WwiseAudio/Events/"
        self._wwise_audio_map: dict[str, list[dict]] = {}  # event_folder → [{debug_name, wem_vfs_path, ...}]
        self._wwise_scan_done = False
        self._pending_wwise_export: tuple[list[dict], str] | None = None  # (entries, output_dir)

        # FMOD bank data (populated by scan after mount)
        self._fmod_banks: dict[str, list[dict]] = {}  # bank_vfs_path → [{index, name, sample_rate, duration_ms}]
        self._fmod_scan_done = False
        self._pending_fmod_export: tuple[list[dict], str] | None = None  # (entries, output_dir)
        # Single temp dir shared by audio + video previews — the MediaPreviewerPanel
        # owns the parent and exposes per-kind subdirs so per-Clear rmtree only
        # nukes one half. Set from main_window.
        self._media_preview_temp_dir: "Path | None" = None
        self._mesh_preview_temp_dir: "Path | None" = None    # set from main_window
        self._tga_preview_temp_dir: "Path | None" = None     # set from main_window
        # Pending temp-export-for-preview state. Tuple is (expected_path, kind, vfs_path)
        # where kind ∈ {"audio", "mesh", "texture", "video"} so _on_export_done
        # can dispatch to the right preview signal once the file lands on disk.
        self._pending_temp_preview: "tuple[str, str, str] | None" = None

        # Type cache populated by `scan_types` after mount. None means the cache
        # hasn't loaded yet — filters that depend on it should treat unknown
        # rows as "any category" so the user isn't blinded during the scan.
        self._type_cache: "TypeCache | None" = None
        self._type_cache_fingerprint: str = ""
        self._type_scan_in_progress: bool = False

        # Folders auto-expanded by the search/filter. Cleared when search
        # empties so we re-collapse only what we opened, not user-opened nodes.
        self._auto_expanded: "set[QTreeWidgetItem]" = set()
        # Folders the user explicitly closed. The filter will not re-open
        # these until the filter is fully cleared.
        self._user_collapsed: "set[QTreeWidgetItem]" = set()
        # True during programmatic setExpanded(False) so _on_item_collapsed
        # doesn't misinterpret it as a user action.
        self._programmatic_collapse: bool = False
        # VFS folders we've already issued a filter-driven browse() for, to
        # avoid re-firing the same lazy-load request before its result lands.
        # Discarded per-folder when the result arrives, fully cleared when
        # the search text empties or the panel re-mounts.
        self._pending_filter_browses: set[str] = set()

        # Debounce timer: coalesces rapid text-input changes into one filter pass.
        self._filter_debounce = QTimer()
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(120)
        self._filter_debounce.timeout.connect(self._filter_tree)

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # Item icons
    # ------------------------------------------------------------------

    # Per-category dot colors — chosen for contrast on dark backgrounds.
    _CAT_COLORS: dict[str, str] = {
        type_cache_mod.CATEGORY_MESH:      "#4a9eff",
        type_cache_mod.CATEGORY_TEXTURE:   "#4dca7a",
        type_cache_mod.CATEGORY_AUDIO:     "#f0a040",
        type_cache_mod.CATEGORY_VIDEO:     "#ff7777",
        type_cache_mod.CATEGORY_MATERIAL:  "#b87fff",
        type_cache_mod.CATEGORY_ANIMATION: "#40d0d0",
        type_cache_mod.CATEGORY_OTHER:     "#888888",
    }

    # Lazy icon caches — populated on first use after QApplication exists.
    _icon_folder: "QIcon | None" = None
    _icon_package: "QIcon | None" = None
    _icon_package_open: "QIcon | None" = None
    _icon_audio_virtual: "QIcon | None" = None
    _icon_export_child: "dict[str, QIcon]" = {}

    @classmethod
    def _get_folder_icon(cls) -> QIcon:
        if cls._icon_folder is None:
            cls._icon_folder = QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_DirIcon
            )
        return cls._icon_folder

    @classmethod
    def _get_package_icon(cls) -> QIcon:
        """Gray rounded-rect with a "+" — a double-clickable, unexpanded
        package. Distinct from the tree's branch arrow, which only appears
        once children exist."""
        if cls._icon_package is None:
            cls._icon_package = cls._make_package_icon(expandable=True)
        return cls._icon_package

    @classmethod
    def _get_package_open_icon(cls) -> QIcon:
        """Plain gray rounded-rect for a package whose exports are loaded."""
        if cls._icon_package_open is None:
            cls._icon_package_open = cls._make_package_icon(expandable=False)
        return cls._icon_package_open

    @staticmethod
    def _make_package_icon(expandable: bool, size: int = 12) -> QIcon:
        px = QPixmap(size, size)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor("#aaaaaa")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(1, 2, size - 2, size - 3, 2, 2)
        if expandable:
            pen = QPen(QColor("#20202e"))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            cx = size / 2.0
            cy = (size + 1) / 2.0
            r = size / 4.5
            p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
            p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        p.end()
        return QIcon(px)

    @classmethod
    def _get_audio_virtual_icon(cls) -> QIcon:
        if cls._icon_audio_virtual is None:
            cls._icon_audio_virtual = cls._make_dot_icon(
                cls._CAT_COLORS[type_cache_mod.CATEGORY_AUDIO]
            )
        return cls._icon_audio_virtual

    @classmethod
    def _get_export_child_icon(cls, export_type: str) -> QIcon:
        cat = type_cache_mod.category_for_export_type(export_type)
        if cat not in cls._icon_export_child:
            cls._icon_export_child[cat] = cls._make_dot_icon(cls._CAT_COLORS.get(cat, "#888888"))
        return cls._icon_export_child[cat]

    @classmethod
    def _icon_for_file(cls, name: str) -> "QIcon | None":
        """Return an icon for a non-folder VFS file based on extension."""
        lower = name.lower()
        if any(lower.endswith(e) for e in (".uasset", ".umap", ".upk")):
            return cls._get_package_icon()
        if any(lower.endswith(e) for e in (".psk", ".pskx")):
            return cls._get_export_child_icon("SkeletalMesh")
        if any(lower.endswith(e) for e in (".tga", ".png", ".dds", ".bmp", ".jpg", ".jpeg")):
            return cls._get_export_child_icon("Texture2D")
        if any(lower.endswith(e) for e in (".wav", ".ogg", ".wem", ".bnk", ".ewem")):
            return cls._get_export_child_icon("SoundWave")
        return None

    @staticmethod
    def _make_dot_icon(color: str, size: int = 12, shape: str = "circle") -> QIcon:
        px = QPixmap(size, size)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(color)))
        p.setPen(Qt.PenStyle.NoPen)
        if shape == "rect":
            p.drawRoundedRect(1, 2, size - 2, size - 3, 2, 2)
        else:
            p.drawEllipse(1, 1, size - 2, size - 2)
        p.end()
        return QIcon(px)

    # ------------------------------------------------------------------
    # Profile support
    # ------------------------------------------------------------------

    @property
    def is_exporting(self) -> bool:
        return self._exporting

    def load_from_profile(self, profile: dict) -> None:
        """Populate UI fields from a profile dict.

        If the user had archives mounted in this session, auto-remount on the
        next event-loop tick so the tree doesn't go visually empty after a
        Manage Profiles edit. The first-ever profile open never auto-mounts —
        the user must opt in by clicking Mount Archives once.
        """
        was_mounted = self._mounted
        self._suppress_export_error_popup = False
        self._game_dir_edit.setText(profile.get("game_dir", ""))

        ue = profile.get("ue_version", "GAME_UE5_4")
        self._ue_version_combo.blockSignals(True)
        idx = self._ue_version_combo.findText(ue)
        if idx >= 0:
            self._ue_version_combo.setCurrentIndex(idx)
        else:
            # Unknown version (e.g. a game-specific EGame enum value not in our list)
            self._ue_version_combo.addItem(ue)
            self._ue_version_combo.setCurrentText(ue)
        self._ue_version_combo.blockSignals(False)

        self._mappings_edit.setText(profile.get("mappings_path", ""))
        self._output_dir_edit.setText(profile.get("unpack_output_dir", ""))

        # AES keys are owned by the profile; we just snapshot them.
        self._aes_keys = list(profile.get("aes_keys", []) or [])

        # Clear mounted state — user must re-mount after switching profiles
        if self._mounted:
            self._mounted = False
            self._tree.clear()
            self._tree.setEnabled(False)
            self._export_btn.setEnabled(False)
            self._export_folder_btn.setEnabled(False)
            self._mount_info.setText("")
            self._status_label.setText("Profile loaded — mount archives to browse")
            # Clear WWise state
            self._wwise_root = ""
            self._wwise_events_prefix = ""
            self._wwise_audio_map = {}
            self._wwise_scan_done = False
            self._pending_wwise_export = None
            # Clear FMOD state
            self._fmod_banks = {}
            self._fmod_scan_done = False
            self._pending_fmod_export = None
            # Drop any in-flight type-cache state — re-mount will re-scan or
            # load the cache for the new game.
            self._type_cache = None
            self._type_cache_fingerprint = ""
            self._type_scan_in_progress = False
            self._type_scan_bar.setVisible(False)
            self._auto_expanded.clear()
            self._user_collapsed.clear()
            self._pending_filter_browses.clear()

        # Auto-remount when the previous profile was already mounted in this
        # session. Defer to the next event-loop tick so the dialog finishes
        # closing before we touch the unpacker QProcess.
        if was_mounted and self._game_dir_edit.text().strip() and self._get_aes_keys():
            self._status_label.setText("Profile changed — remounting...")
            QTimer.singleShot(0, self._mount_archives)

    def collect_for_profile(self) -> dict:
        """Collect current UI state as a dict fragment for saving to a profile."""
        return {
            "game_dir": self._game_dir_edit.text().strip(),
            "ue_version": self._ue_version_combo.currentText(),
            "mappings_path": self._mappings_edit.text().strip(),
            "unpack_output_dir": self._output_dir_edit.text().strip(),
            "aes_keys": self._get_aes_keys(),
        }

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # ── Top toolbar: read-only mount summary + Mount button ───────
        # Editing of game folder / UE version / mappings lives in the
        # Manage Profiles dialog only. This strip just shows what the
        # active profile is and lets the user kick off a mount.
        toolbar = QFrame()
        toolbar.setFrameShape(QFrame.Shape.NoFrame)
        toolbar.setProperty("cssClass", "toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 4)

        toolbar_layout.addWidget(QLabel("Game folder:"))
        self._game_dir_edit = QLineEdit()
        self._game_dir_edit.setPlaceholderText("(set via Manage Profiles)")
        self._game_dir_edit.setReadOnly(True)
        self._game_dir_edit.setProperty("cssClass", "readonly")
        self._game_dir_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        toolbar_layout.addWidget(self._game_dir_edit, 3)

        toolbar_layout.addWidget(QLabel("UE:"))
        self._ue_version_combo = QComboBox()
        self._ue_version_combo.setEditable(False)
        self._ue_version_combo.addItems(_UE_VERSIONS)
        saved_ver = config.get("unpack_ue_version")
        if saved_ver in _UE_VERSIONS:
            self._ue_version_combo.setCurrentText(saved_ver)
        else:
            self._ue_version_combo.setCurrentText("GAME_UE5_4")
        self._ue_version_combo.setEnabled(False)
        self._ue_version_combo.setFixedWidth(180)
        toolbar_layout.addWidget(self._ue_version_combo)

        # Mappings path is sourced from the profile and consumed by
        # _mount_archives — kept as a hidden QLineEdit for state-storage
        # compatibility, never shown in the toolbar.
        self._mappings_edit = QLineEdit()
        self._mappings_edit.setVisible(False)

        toolbar_layout.addStretch()

        self._mount_btn = QPushButton("Mount Archives")
        self._mount_btn.setProperty("cssClass", "success")
        self._mount_btn.clicked.connect(self._mount_archives)
        toolbar_layout.addWidget(self._mount_btn)

        layout.addWidget(toolbar)

        # ── Search row ────────────────────────────────────────────────
        search_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter tree by name...")
        self._search.textChanged.connect(self._filter_debounce.start)
        search_row.addWidget(self._search, 1)
        layout.addLayout(search_row)

        # ── Category filter checkboxes (directly under search bar) ───
        cat_row = QHBoxLayout()
        cat_row.setContentsMargins(0, 2, 0, 2)
        cat_row.addWidget(QLabel("Categories:"))
        self._cat_checkboxes: dict[str, QCheckBox] = {}
        for cat_id, label in (
            (type_cache_mod.CATEGORY_MESH, "Meshes"),
            (type_cache_mod.CATEGORY_TEXTURE, "Textures"),
            (type_cache_mod.CATEGORY_AUDIO, "Audio"),
            (type_cache_mod.CATEGORY_VIDEO, "Video"),
            (type_cache_mod.CATEGORY_MATERIAL, "Materials"),
            (type_cache_mod.CATEGORY_ANIMATION, "Animations"),
            (type_cache_mod.CATEGORY_OTHER, "Other"),
        ):
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.stateChanged.connect(self._filter_tree)
            cat_row.addWidget(cb)
            self._cat_checkboxes[cat_id] = cb
        cat_row.addStretch()
        layout.addLayout(cat_row)

        # ── Mount info (middle area) ──────────────────────────────────
        self._mount_info = QLabel("")
        layout.addWidget(self._mount_info)

        # ── Type-scan progress bar (only visible while background scan runs) ──
        self._type_scan_bar = QProgressBar()
        self._type_scan_bar.setTextVisible(True)
        self._type_scan_bar.setFormat("Scanning asset types: %v / %m packages")
        self._type_scan_bar.setMaximumHeight(18)
        self._type_scan_bar.setVisible(False)
        layout.addWidget(self._type_scan_bar)

        # ── VFS Tree ──────────────────────────────────────────────────
        self._tree = ZoomableTree()
        self._tree.setHeaderLabels(["Name"])
        # Single-column view — the header strip is dead space, hide it.
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemCollapsed.connect(self._on_item_collapsed)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemSelectionChanged.connect(self._maybe_expand_export_section)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.setEnabled(False)
        layout.addWidget(self._tree, stretch=1)

        # ── Progress bar (always visible above the export panel) ─────
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setFormat("%v / %m (%p%)")
        layout.addWidget(self._progress)

        self._status_label = QLabel("Not mounted")
        layout.addWidget(self._status_label)

        # ── Export controls (collapsible footer) ─────────────────────
        # Auto-expands on first selection so users don't fish for it.
        self._export_section = CollapsibleSection("Export", start_expanded=False)
        export_layout = QVBoxLayout()

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Mounted Folder:"))
        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setPlaceholderText("Where mounted/exported files land — also where PSK Picker reads from")
        self._output_dir_edit.setText(config.get("unpack_output_dir"))
        out_row.addWidget(self._output_dir_edit)
        self._browse_output_btn = QPushButton("Browse...")
        self._browse_output_btn.setFixedWidth(80)
        self._browse_output_btn.clicked.connect(self._browse_output_dir)
        out_row.addWidget(self._browse_output_btn)
        export_layout.addLayout(out_row)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Formats:"))
        self._chk_mesh = QCheckBox("Meshes (PSK)")
        self._chk_mesh.setChecked(True)
        fmt_row.addWidget(self._chk_mesh)
        self._chk_texture = QCheckBox("Textures (PNG)")
        self._chk_texture.setChecked(True)
        fmt_row.addWidget(self._chk_texture)
        self._chk_anim = QCheckBox("Animations (PSA)")
        self._chk_anim.setChecked(True)
        fmt_row.addWidget(self._chk_anim)
        self._chk_audio = QCheckBox("Audio (OGG/WAV)")
        self._chk_audio.setChecked(True)
        fmt_row.addWidget(self._chk_audio)
        self._chk_props = QCheckBox("Props (TXT)")
        self._chk_props.setChecked(True)
        fmt_row.addWidget(self._chk_props)
        fmt_row.addStretch()
        export_layout.addLayout(fmt_row)

        btn_row = QHBoxLayout()
        self._export_btn = QPushButton("Export Selected")
        self._export_btn.setEnabled(False)
        self._export_btn.setProperty("cssClass", "accent")
        self._export_btn.clicked.connect(self._export_selected)
        btn_row.addWidget(self._export_btn)

        self._export_folder_btn = QPushButton("Export Folder")
        self._export_folder_btn.setEnabled(False)
        self._export_folder_btn.setProperty("cssClass", "accent")
        self._export_folder_btn.clicked.connect(self._export_folder)
        btn_row.addWidget(self._export_folder_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_export)
        btn_row.addWidget(self._cancel_btn)

        btn_row.addStretch()

        self._handoff_btn = QPushButton("Send PSKs to Queue →")
        self._handoff_btn.setEnabled(False)
        self._handoff_btn.setToolTip(
            "Send the PSK/PSKX files extracted in this session to the Asset "
            "Browser queue. Only files this session actually wrote are "
            "included — pre-existing files in the output folder are left alone."
        )
        self._handoff_btn.clicked.connect(self._handoff_psks)
        btn_row.addWidget(self._handoff_btn)

        export_layout.addLayout(btn_row)
        self._export_section.set_content_layout(export_layout)
        layout.addWidget(self._export_section)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self._unpacker.initialized.connect(self._on_initialized)
        self._unpacker.browse_result.connect(self._on_browse_result)
        self._unpacker.progress.connect(self._on_progress)
        self._unpacker.export_done.connect(self._on_export_done)
        self._unpacker.props_result.connect(self._on_props_result)
        self._unpacker.exports_listed.connect(self._on_exports_listed)
        self._unpacker.wwise_scan_result.connect(self._on_wwise_scan_result)
        self._unpacker.fmod_scan_result.connect(self._on_fmod_scan_result)
        self._unpacker.types_scan_progress.connect(self._on_types_scan_progress)
        self._unpacker.types_scan_batch.connect(self._on_types_scan_batch)
        self._unpacker.types_scan_done.connect(self._on_types_scan_done)
        self._unpacker.warning.connect(self._on_warning)
        self._unpacker.version_warning.connect(self._on_version_warning)
        self._unpacker.version_detected.connect(self._on_version_detected)
        self._unpacker.error.connect(self._on_error)
        self._unpacker.process_ended.connect(self._on_process_ended)

    # ------------------------------------------------------------------
    # Tree filtering (in-place hide/show for lazy-loaded VFS)
    # ------------------------------------------------------------------

    # Extension-based fallback for files we don't have export-type info on.
    _MESH_FILE_EXTS = (".psk", ".pskx")
    _TEXTURE_FILE_EXTS = (".tga", ".png", ".dds", ".bmp", ".jpg", ".jpeg")
    _AUDIO_FILE_EXTS = (".wav", ".ogg", ".wem", ".bnk", ".ewem", ".bank")
    _PACKAGE_FILE_EXTS = (".uasset", ".upk", ".umap")

    def _row_categories(self, item: QTreeWidgetItem) -> "set[str] | None":
        """Return the categories this row belongs to, or None for 'unknown — pass'.

        - WWise virtual rows → audio
        - Export rows (UserRole+4) → from cached export_type taxonomy
        - Plain files → cached type info first, then file extension
        - Unscanned packages while a scan is in progress → None (fail open)
        - Folders → None (visibility decided by descendants)
        """
        if item.data(0, Qt.ItemDataRole.UserRole + 2):
            return {type_cache_mod.CATEGORY_AUDIO}

        export_type = item.data(0, Qt.ItemDataRole.UserRole + 4)
        if export_type:
            return {type_cache_mod.category_for_export_type(str(export_type))}

        if item.data(0, Qt.ItemDataRole.UserRole + 1):
            return None

        vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        lower = vfs_path.lower()

        if any(lower.endswith(ext) for ext in self._PACKAGE_FILE_EXTS):
            # If the package has been expanded, derive categories from real export
            # children (UserRole+4) — more accurate than the heuristic cache entry.
            if item.childCount() > 0:
                child_cats: set[str] = set()
                for i in range(item.childCount()):
                    ch = item.child(i)
                    exp_type = ch.data(0, Qt.ItemDataRole.UserRole + 4)
                    if exp_type:
                        child_cats.add(type_cache_mod.category_for_export_type(str(exp_type)))
                if child_cats:
                    return child_cats
            if self._type_cache is not None:
                cats = self._type_cache.categories_for_package(vfs_path)
                if cats:
                    return cats
            # Mid-scan or pre-cache: use the asset_type the CLI stamped on the
            # browse_result for this row. Same heuristic the cache will emit
            # once its batch lands, so the user sees stable filter behavior.
            asset_type = item.data(0, Qt.ItemDataRole.UserRole + 5)
            if asset_type:
                return {type_cache_mod.category_for_asset_type(str(asset_type))}
            # No info at all — default to Other so category filters remain
            # meaningful (fail-open would let everything bleed into Mesh etc.)
            return {type_cache_mod.CATEGORY_OTHER}

        if lower.endswith(self._MESH_FILE_EXTS):
            return {type_cache_mod.CATEGORY_MESH}
        if lower.endswith(self._TEXTURE_FILE_EXTS):
            return {type_cache_mod.CATEGORY_TEXTURE}
        if lower.endswith(self._AUDIO_FILE_EXTS):
            return {type_cache_mod.CATEGORY_AUDIO}

        return {type_cache_mod.CATEGORY_OTHER}

    def _filter_is_active(self, text: str, active_cats: set) -> bool:
        return bool(text) or active_cats != type_cache_mod.ALL_CATEGORIES

    def _filter_tree(self):
        """Filter the VFS tree in-place, hiding non-matching items."""
        text = self._search.text().strip().lower()
        active_cats = {
            cat_id for cat_id, cb in self._cat_checkboxes.items() if cb.isChecked()
        }

        is_active = self._filter_is_active(text, active_cats)

        # Batch all visibility changes to avoid per-item repaints stalling the
        # event loop on large trees.
        root = self._tree.invisibleRootItem()
        self._tree.setUpdatesEnabled(False)
        try:
            self._filter_tree_recursive(
                root, text, active_cats,
                parent_name_match=False, is_active=is_active,
            )
        finally:
            self._tree.setUpdatesEnabled(True)

        # When no filter is active, restore folders we auto-opened and drop
        # any in-flight lazy-load tracking.
        if not is_active:
            if self._auto_expanded:
                self._tree.setUpdatesEnabled(False)
                self._programmatic_collapse = True
                try:
                    for opened in list(self._auto_expanded):
                        try:
                            opened.setExpanded(False)
                        except RuntimeError:
                            pass  # item may have been deleted between filter calls
                finally:
                    self._programmatic_collapse = False
                    self._tree.setUpdatesEnabled(True)
                self._auto_expanded.clear()
                self._user_collapsed.clear()
            self._pending_filter_browses.clear()

        # Reveal name matches that live inside still-collapsed (lazy-loaded)
        # folders by issuing targeted browse() calls. Each result re-runs
        # _filter_tree which descends one more level — see _request_lazy_loads_for_filter.
        if is_active and text and self._type_cache is not None:
            self._request_lazy_loads_for_filter(text)

    def _request_lazy_loads_for_filter(self, text: str, max_per_pass: int = 25) -> None:
        """Auto-expand placeholder folders along paths to type-cache name matches.

        The tree is lazy: each folder only fetches its children on first expand.
        That means a name search for an asset whose ancestors are still collapsed
        finds nothing — the matching item simply doesn't exist as a tree row yet.

        The post-mount type cache holds every package's full VFS path, so we can
        identify which packages match and walk their ancestor chains, browsing
        the shallowest placeholder folder we hit. Each browse result re-fires
        _filter_tree, which descends one more level. After ~depth passes the
        match becomes visible. Cap per-pass to keep the CLI from getting flooded
        on broad searches.
        """
        cache = self._type_cache
        if cache is None or not text:
            return

        ancestor_folders: set[str] = set()
        for path, exports in cache.entries.items():
            basename = path.rsplit("/", 1)[-1].lower()
            hit = text in basename
            if not hit:
                for exp in exports:
                    name = (exp.get("name") or "").lower()
                    if name and text in name:
                        hit = True
                        break
            if not hit:
                continue
            parts = path.split("/")
            for depth in range(1, len(parts)):
                ancestor_folders.add("/".join(parts[:depth]))

        if not ancestor_folders:
            return

        triggered = 0
        for folder in sorted(ancestor_folders, key=lambda f: f.count("/")):
            if triggered >= max_per_pass:
                break
            if folder in self._pending_filter_browses:
                continue
            item = self._find_tree_item(folder)
            if item is None:
                continue
            if item in self._user_collapsed:
                continue
            if item.childCount() != 1:
                continue
            only_child = item.child(0)
            if only_child.data(0, Qt.ItemDataRole.UserRole) != _PLACEHOLDER:
                continue
            self._pending_filter_browses.add(folder)
            self._unpacker.browse(folder)
            triggered += 1

    def _filter_tree_recursive(
        self,
        parent: QTreeWidgetItem,
        text: str,
        active_cats: set,
        parent_name_match: bool,
        is_active: bool,
    ) -> bool:
        """Hide non-matching items. Returns True if any child is visible."""
        if not is_active:
            # Fast path: show everything without classifying each row.
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.isHidden():
                    child.setHidden(False)
                if child.data(0, Qt.ItemDataRole.UserRole + 1):  # is folder
                    self._filter_tree_recursive(
                        child, text, active_cats,
                        parent_name_match=False, is_active=False,
                    )
            return True

        any_visible = False
        for i in range(parent.childCount()):
            child = parent.child(i)
            is_folder = bool(child.data(0, Qt.ItemDataRole.UserRole + 1))
            display = child.text(0)
            # Strip "[Type]" suffix so a search like "_TX" doesn't accidentally
            # match "[Texture2D]" rows just because the type label shares letters.
            base = display.rsplit("[", 1)[0].rstrip().lower()

            self_name_match = (not text) or (text in base)
            name_match = self_name_match or parent_name_match

            if is_folder:
                # Never auto-expand a lazy-loaded (placeholder-only) folder.
                # Expanding it fires itemExpanded → browse() → _on_browse_result
                # → _filter_tree(), cascading into thousands of CLI round-trips.
                # Show/hide the folder by name; the user can expand it manually.
                is_placeholder_only = (
                    child.childCount() == 1
                    and child.child(0).data(0, Qt.ItemDataRole.UserRole) == _PLACEHOLDER
                )
                if is_placeholder_only:
                    name_passes = bool(self_name_match or parent_name_match)
                    type_passes = True
                    if active_cats != type_cache_mod.ALL_CATEGORIES and self._type_cache is not None:
                        folder_path = child.data(0, Qt.ItemDataRole.UserRole) or ""
                        folder_cats = self._type_cache.categories_under_folder(folder_path)
                        # Only hide if we have indexed data; unknown folders stay visible.
                        if folder_cats:
                            type_passes = bool(folder_cats & active_cats)
                    visible = name_passes and type_passes
                    child.setHidden(not visible)
                    if visible:
                        any_visible = True
                    continue

                descendant_visible = self._filter_tree_recursive(
                    child, text, active_cats,
                    parent_name_match=parent_name_match or self_name_match,
                    is_active=is_active,
                )
                visible = descendant_visible
                child.setHidden(not visible)
                if visible:
                    any_visible = True
                    if not child.isExpanded() and child not in self._user_collapsed:
                        child.setExpanded(True)
                        self._auto_expanded.add(child)
                continue

            # Non-folder rows: combine name axis with category filter.
            type_match = True
            if active_cats != type_cache_mod.ALL_CATEGORIES:
                cats = self._row_categories(child)
                if cats is not None and not (cats & active_cats):
                    type_match = False

            visible = name_match and type_match
            child.setHidden(not visible)
            if visible:
                any_visible = True

        return any_visible

    # ------------------------------------------------------------------
    # Mount
    # ------------------------------------------------------------------

    def _mount_archives(self):
        cli_path = config.get_cue4parse_cli()
        if not cli_path:
            QMessageBox.warning(self, "CLI Not Configured",
                                "Set the CUE4ParseCLI path in Settings first.")
            return

        game_dir = self._game_dir_edit.text().strip()
        if not game_dir:
            QMessageBox.warning(
                self, "No Game Directory",
                "Open Manage Profiles and set the Game folder for this profile.",
            )
            return

        ue_version = self._ue_version_combo.currentText()
        config.set("unpack_ue_version", ue_version)

        self._mount_btn.setEnabled(False)
        self._status_label.setText("Starting CUE4ParseCLI...")
        self._progress.setMaximum(0)  # indeterminate
        # Clear any prior version-mismatch banner — re-mount re-arms detection.
        self.version_mismatch.emit("")

        if not self._unpacker.is_running:
            if not self._unpacker.start(cli_path):
                self._mount_btn.setEnabled(True)
                self._status_label.setText("Failed to start CLI")
                self._progress.setMaximum(1)
                return

        aes_keys = self._get_aes_keys()
        mappings = self._mappings_edit.text().strip()
        self._unpacker.initialize(game_dir, aes_keys, ue_version, mappings)

    @Slot(int, int, int, int, int, list)
    def _on_initialized(self, archive_count: int, unmounted_count: int,
                        file_count: int, keys_submitted: int,
                        loose_file_count: int = 0,
                        unmounted_archives: list | None = None):
        # Defensive: clamp negative counts (CLI bug shouldn't take down the GUI)
        archive_count = max(0, archive_count)
        unmounted_count = max(0, unmounted_count)
        file_count = max(0, file_count)
        keys_submitted = max(0, keys_submitted)
        loose_file_count = max(0, loose_file_count)
        unmounted_archives = unmounted_archives or []

        self._mounted = True
        self._mount_btn.setEnabled(True)
        self._tree.setEnabled(True)
        self._export_btn.setEnabled(True)
        self._export_folder_btn.setEnabled(True)
        self._progress.setMaximum(1)
        self._progress.setValue(0)

        info = f"Mounted: {archive_count} archives, {file_count:,} files"
        if loose_file_count:
            info += f" ({loose_file_count:,} loose .upk/.wem/.ewem/.bnk/.bank)"
        if unmounted_count:
            info += f" ({unmounted_count} archives need AES keys)"
        if keys_submitted:
            info += f", {keys_submitted} AES key(s) applied"
        self._mount_info.setText(info)
        self._status_label.setText("Ready — browse the tree and select assets to export")
        self.log_message.emit(info, "success")

        # Populate root of VFS tree
        self._tree.clear()
        self._auto_expanded.clear()
        self._user_collapsed.clear()
        self._pending_filter_browses.clear()
        self._unpacker.browse("")

        # Surface a key-entry prompt unless the caller is itself a remount
        # triggered by the prompt (avoids re-prompting on a wrong-key paste).
        if unmounted_count > 0 and not self._aes_prompt_pending:
            self._aes_prompt_pending = True
            self.aes_keys_required.emit(unmounted_count, list(unmounted_archives))
        else:
            self._aes_prompt_pending = False

        # Trigger WWise audio event scan (CLI returns quickly if no WWiseAudio folder)
        self._wwise_root = ""
        self._wwise_events_prefix = ""
        self._wwise_audio_map = {}
        self._wwise_scan_done = False
        self._unpacker.scan_wwise_events()
        # The FMOD scan is NOT sent here — while the wwise scan runs, the CLI's
        # cancel-support reader discards unrecognized commands, so a back-to-back
        # scan_fmod_banks would be silently dropped. _on_wwise_scan_result sends
        # it once the wwise scan has actually finished.
        self._fmod_banks = {}
        self._fmod_scan_done = False

        # Try to reuse a cached type map for this game; otherwise scan fresh.
        self._begin_type_cache_load()

    # ------------------------------------------------------------------
    # Type cache (asset-class index used by the Filters section)
    # ------------------------------------------------------------------

    def _begin_type_cache_load(self) -> None:
        """Load the type cache from disk if available; else trigger a scan."""
        game_dir = self._game_dir_edit.text().strip()
        ue_version = self._ue_version_combo.currentText()
        fingerprint = type_cache_mod.compute_fingerprint(game_dir, ue_version)
        self._type_cache_fingerprint = fingerprint

        cached = TypeCache.load(fingerprint)
        if cached is not None:
            self._type_cache = cached
            self._type_scan_in_progress = False
            self._type_scan_bar.setVisible(False)
            self.log_message.emit(
                f"Type cache loaded ({len(cached.entries):,} packages)", "info"
            )
            self._filter_tree()
            return

        # Cache miss — start a fresh scan. The user can browse/search while
        # it runs; filters that depend on the cache treat unknown rows as
        # "any category" until done.
        self._type_cache = TypeCache()
        self._type_scan_in_progress = True
        self._type_scan_log_pct = -1  # tracks last logged 10% milestone
        self._type_scan_bar.setValue(0)
        self._type_scan_bar.setMaximum(0)  # indeterminate until total is known
        self._type_scan_bar.setVisible(True)
        self.log_message.emit("Asset-type scan started", "info")
        self._unpacker.scan_types()

    @Slot(int, int)
    def _on_types_scan_progress(self, current: int, total: int) -> None:
        if total > 0:
            self._type_scan_bar.setMaximum(total)
            self._type_scan_bar.setValue(current)
            # Log every 10% milestone to keep the user informed without spamming.
            pct = (current * 10) // total
            if pct > getattr(self, "_type_scan_log_pct", -1):
                self._type_scan_log_pct = pct
                self.log_message.emit(
                    f"Asset-type scan: {current:,}/{total:,} packages ({pct * 10}%)", "info"
                )

    @Slot(list, bool)
    def _on_types_scan_batch(self, entries: list, is_final: bool) -> None:
        if self._type_cache is None:
            self._type_cache = TypeCache()
        self._type_cache.add_batch(entries)

    @Slot(int, int)
    def _on_types_scan_done(self, error_count: int, total_packages: int) -> None:
        self._type_scan_in_progress = False
        self._type_scan_bar.setVisible(False)
        if self._type_cache is None:
            self._type_cache = TypeCache()
        self._type_cache.error_count = error_count
        self._type_cache.total_packages = total_packages

        try:
            self._type_cache.save(self._type_cache_fingerprint)
        except OSError as exc:
            log.warning("Could not save type cache: %s", exc)

        self._type_cache.rebuild_folder_index()

        msg = f"Asset-type scan complete ({len(self._type_cache.entries):,} packages"
        if error_count:
            msg += f", {error_count} unreadable"
        msg += ")"
        self.log_message.emit(msg, "info")

        # Apply any active filter now that we have full type info.
        self._filter_tree()

    # ------------------------------------------------------------------
    # WWise audio scan
    # ------------------------------------------------------------------

    @Slot(dict)
    def _on_wwise_scan_result(self, result: dict):
        """Handle the result of scanning AkAudioEvent assets."""
        self._wwise_scan_done = True

        # Chain the FMOD scan behind the wwise scan — sending it any earlier
        # gets it dropped by the CLI's during-export command reader.
        if not self._fmod_scan_done:
            self._unpacker.scan_fmod_banks()

        if not result.get("found"):
            return

        self._wwise_root = result.get("wwise_root", "")
        self._wwise_events_prefix = result.get("events_prefix", "")
        total_audio = result.get("total_audio", 0)
        total_events = result.get("total_events", 0)

        # Build audio map: event_folder → list of audio entries
        self._wwise_audio_map = {}
        for entry in result.get("audio", []):
            folder = entry.get("event_folder", "")
            self._wwise_audio_map.setdefault(folder, []).append(entry)

        msg = f"WWise scan: {total_audio} audio files from {total_events} events"
        self._status_label.setText(msg)
        self.log_message.emit(msg, "success")

        # If the WwiseAudio folder is already visible in the tree, refresh it
        self._refresh_wwise_tree_node()

    def _refresh_wwise_tree_node(self):
        """If any WwiseAudio node is in the tree, clear and re-browse it to apply filtering."""
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            child = root.child(i)
            self._refresh_wwise_recursive(child)

    def _refresh_wwise_recursive(self, item: QTreeWidgetItem):
        """Walk tree to find and refresh any WwiseAudio folder node."""
        name = item.text(0)
        is_folder = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if is_folder and name.lower() == "wwiseaudio":
            # Clear children and re-browse to apply filtering
            vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            while item.childCount():
                item.removeChild(item.child(0))
            # Add placeholder and re-browse
            placeholder = QTreeWidgetItem(["Loading..."])
            placeholder.setData(0, Qt.ItemDataRole.UserRole, _PLACEHOLDER)
            item.addChild(placeholder)
            self._unpacker.browse(vfs_path)
            return
        # Recurse into children
        for i in range(item.childCount()):
            self._refresh_wwise_recursive(item.child(i))

    # ------------------------------------------------------------------
    # FMOD bank scan
    # ------------------------------------------------------------------

    @Slot(dict)
    def _on_fmod_scan_result(self, result: dict):
        """Handle the result of scanning FMOD .bank files for streams."""
        self._fmod_scan_done = True
        if not result.get("found"):
            return

        self._fmod_banks = {}
        for bank in result.get("banks", []):
            bank_path = bank.get("bank_vfs_path", "")
            streams = bank.get("streams", [])
            if bank_path and streams:
                self._fmod_banks[bank_path] = streams

        total_banks = result.get("total_banks", 0)
        total_streams = result.get("total_streams", 0)
        msg = f"FMOD scan: {total_streams} streams in {total_banks} banks"
        self._status_label.setText(msg)
        self.log_message.emit(msg, "success")

        # Inject stream rows under any bank items already in the tree.
        self._tree.setUpdatesEnabled(False)
        try:
            stack = [self._tree.invisibleRootItem()]
            while stack:
                parent = stack.pop()
                for i in range(parent.childCount()):
                    child = parent.child(i)
                    vfs_path = child.data(0, Qt.ItemDataRole.UserRole) or ""
                    if vfs_path in self._fmod_banks:
                        self._inject_fmod_streams(child)
                    elif child.childCount():
                        stack.append(child)
        finally:
            self._tree.setUpdatesEnabled(True)

    @staticmethod
    def _fmod_bank_folder(bank_path: str) -> str:
        """Output subfolder for a bank's streams: the bank path minus .bank."""
        p = bank_path[:-5] if bank_path.lower().endswith(".bank") else bank_path
        return p.strip("/")

    @staticmethod
    def _fmod_stream_name(bank_path: str, stream: dict) -> str:
        """Stable, collision-free filename stem for a bank stream.

        The FSB name table is optional and names may repeat, so the 1-based
        stream index is always part of the stem.
        """
        idx = int(stream.get("index", 0) or 0)
        name = (stream.get("name") or "").strip()
        if name:
            return f"{idx:04d}_{name}"
        stem = bank_path.rsplit("/", 1)[-1]
        if stem.lower().endswith(".bank"):
            stem = stem[:-5]
        return f"{stem}_{idx:04d}"

    @staticmethod
    def _fs_sanitize(segment: str) -> str:
        """Mirror the CLI's SanitizeName (invalid chars + spaces → '_') so
        cache-path predictions match what actually lands on disk."""
        cleaned = "".join(
            "_" if (c in '<>:"/\\|?*' or c == " " or ord(c) < 32) else c
            for c in segment)
        return cleaned.strip("_")

    def _fmod_disk_relpath(self, bank_path: str, stream: dict) -> Path:
        """Relative disk path the CLI will write this stream's WAV to."""
        folder = self._fmod_bank_folder(bank_path)
        parts = [self._fs_sanitize(p) for p in folder.split("/") if p]
        name = self._fs_sanitize(self._fmod_stream_name(bank_path, stream))
        return Path(*parts) / f"{name}.wav"

    def _fmod_entry(self, bank_path: str, stream: dict) -> dict:
        return {
            "bank_vfs_path": bank_path,
            "stream_index": int(stream.get("index", 0) or 0),
            "target_name": self._fmod_stream_name(bank_path, stream),
            "target_folder": self._fmod_bank_folder(bank_path),
        }

    def _inject_fmod_streams(self, bank_item: QTreeWidgetItem):
        """Add virtual stream child rows under a .bank tree item."""
        bank_path = bank_item.data(0, Qt.ItemDataRole.UserRole) or ""
        streams = self._fmod_banks.get(bank_path, [])
        if not streams or bank_item.childCount():
            return
        for stream in streams:
            display = f"{self._fmod_stream_name(bank_path, stream)}.wav"
            child = QTreeWidgetItem([display])
            child.setData(0, Qt.ItemDataRole.UserRole, bank_path)
            child.setData(0, Qt.ItemDataRole.UserRole + 1, False)  # not a folder
            child.setData(0, Qt.ItemDataRole.UserRole + 2,
                          {"bank_vfs_path": bank_path, **stream})
            child.setForeground(0, Qt.GlobalColor.cyan)
            child.setIcon(0, self._get_audio_virtual_icon())
            bank_item.addChild(child)

    @Slot(str, list)
    def _on_browse_result(self, path: str, entries: list):
        # Filter-driven lazy-load completed for this folder; allow future passes
        # to issue fresh browses if the user changes the search again.
        self._pending_filter_browses.discard(path)
        if not path:
            # Root level
            parent = self._tree.invisibleRootItem()
        else:
            parent = self._find_tree_item(path)
            if parent is None:
                return

        # Remove placeholder children
        for i in range(parent.childCount() - 1, -1, -1):
            child = parent.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole) == _PLACEHOLDER:
                parent.removeChild(child)

        # ── WWise filtering: hide Event and Media folders ─────────────
        if self._wwise_scan_done and self._wwise_root:
            wwise_parent = self._wwise_root.rstrip("/")
            if path == wwise_parent:
                entries = [e for e in entries
                           if not (e.get("is_folder") and
                                   e["name"].lower() in ("event", "media"))]

        # Sort: folders first, then files
        folders = sorted([e for e in entries if e.get("is_folder")], key=lambda e: e["name"].lower())
        files = sorted([e for e in entries if not e.get("is_folder")], key=lambda e: e["name"].lower())

        for entry in folders:
            item = QTreeWidgetItem([entry["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, self._make_path(path, entry["name"]))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, True)  # is_folder
            item.setIcon(0, self._get_folder_icon())
            # Add placeholder for lazy loading
            placeholder = QTreeWidgetItem(["Loading..."])
            placeholder.setData(0, Qt.ItemDataRole.UserRole, _PLACEHOLDER)
            item.addChild(placeholder)
            parent.addChild(item)

        for entry in files:
            item = QTreeWidgetItem([entry["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, self._make_path(path, entry["name"]))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, False)  # is_folder
            # Stash the CLI's heuristic asset_type so _row_categories has a
            # same-quality fallback during a type scan (the cache won't have
            # this package's entry until its batch lands).
            asset_type = entry.get("asset_type")
            if asset_type:
                item.setData(0, Qt.ItemDataRole.UserRole + 5, asset_type)
            icon = self._icon_for_file(entry["name"])
            if icon:
                item.setIcon(0, icon)
            parent.addChild(item)
            if self._fmod_scan_done:
                self._inject_fmod_streams(item)  # no-op unless a scanned .bank

        # ── WWise: inject virtual audio items into Events subfolders ──
        if self._wwise_scan_done and self._wwise_events_prefix:
            events_path = self._wwise_events_prefix.rstrip("/")
            if path == events_path or path.startswith(events_path + "/"):
                # Compute the relative folder within Events
                if path == events_path:
                    rel_folder = ""
                else:
                    rel_folder = path[len(events_path) + 1:]

                audio_format = config.get("export_audio_format") or "wav"
                audio_entries = self._wwise_audio_map.get(rel_folder, [])
                for audio in audio_entries:
                    display = f"{audio['debug_name']}.{audio_format}"
                    item = QTreeWidgetItem([display])
                    # Store the wem VFS path for export lookup
                    item.setData(0, Qt.ItemDataRole.UserRole, audio["wem_vfs_path"])
                    item.setData(0, Qt.ItemDataRole.UserRole + 1, False)  # not a folder
                    item.setData(0, Qt.ItemDataRole.UserRole + 2, audio)  # audio metadata
                    item.setForeground(0, Qt.GlobalColor.cyan)
                    item.setIcon(0, self._get_audio_virtual_icon())
                    parent.addChild(item)

        # Re-apply filter only when one is active — the full tree walk is
        # expensive and unnecessary when everything is already visible.
        if self._filter_is_active(
            self._search.text().strip().lower(),
            {k for k, cb in self._cat_checkboxes.items() if cb.isChecked()},
        ):
            self._filter_tree()

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    _IMAGE_EXTS = frozenset({".tga", ".png", ".dds", ".bmp", ".jpg", ".jpeg"})
    _AUDIO_EXTS = frozenset({".wav", ".ogg", ".wem", ".bnk", ".ewem"})
    _MESH_EXTS = frozenset({".psk", ".pskx"})
    _VIDEO_EXTS = frozenset({".bk2", ".mp4", ".webm", ".mov"})
    _PACKAGE_EXTS_FOR_MESH = (".uasset", ".upk", ".umap")

    def _classify_row(self, item: QTreeWidgetItem) -> str:
        """Identify what kind of preview a tree row supports.

        Returns one of "mesh", "texture", "audio", "video", "package", "unknown".
        - audio_data set            → "audio" (WWise virtual entry)
        - export_type set           → mapped to mesh/texture/audio/video or "unknown"
        - file with mesh/audio/img/video extension → that type
        - .uasset/.upk/.umap that hasn't been expanded yet → "package"
          (the user can preview as mesh/texture/audio/video without expanding;
          the CLI runs a single-format export and we surface what landed)
        - everything else           → "unknown"

        Export-type sets live in core/type_cache so the filter category logic
        and the preview menu agree on which UE classes are previewable.
        """
        if item.data(0, Qt.ItemDataRole.UserRole + 2):
            return "audio"

        export_type = item.data(0, Qt.ItemDataRole.UserRole + 4)
        if export_type:
            if export_type in type_cache_mod.MESH_EXPORT_TYPES:
                return "mesh"
            if export_type in type_cache_mod.TEXTURE_EXPORT_TYPES:
                return "texture"
            if export_type in type_cache_mod.AUDIO_EXPORT_TYPES:
                return "audio"
            if export_type in type_cache_mod.VIDEO_EXPORT_TYPES:
                return "video"
            return "unknown"

        vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        suffix = Path(vfs_path.lower()).suffix
        if suffix in self._AUDIO_EXTS:
            return "audio"
        if suffix in self._IMAGE_EXTS:
            return "texture"
        if suffix in self._MESH_EXTS:
            return "mesh"
        if suffix in self._VIDEO_EXTS:
            return "video"
        if suffix in self._PACKAGE_EXTS_FOR_MESH:
            return "package"
        return "unknown"

    def _show_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        self._popup_context_menu(item, self._tree.viewport().mapToGlobal(pos))

    def _popup_context_menu(self, item: QTreeWidgetItem | None, global_pos):
        """Build and show the right-click menu for *item*. Split out from
        `_show_context_menu` so tests can drive it directly without poking
        the QTreeWidget's internal hit-testing."""
        vfs_path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        is_placeholder = vfs_path == _PLACEHOLDER
        is_folder = bool(item.data(0, Qt.ItemDataRole.UserRole + 1)) if item is not None else False

        # Folder rows / placeholders / blank-area clicks only get the
        # expand/collapse helpers — there's no individual asset to act on.
        if item is None or is_placeholder or is_folder:
            menu = QMenu(self)
            add_tree_expand_actions(menu, self._tree, item)
            if menu.actions():
                menu.exec(global_pos)
            return

        audio_data = item.data(0, Qt.ItemDataRole.UserRole + 2)
        kind = self._classify_row(item)

        menu = QMenu(self)

        # Type-specific preview action(s) — temp-exports if needed, so we
        # never have to disable the menu item with a "must export first"
        # tooltip.
        def _add_mesh():
            act = QAction("Preview Mesh", self)
            act.triggered.connect(lambda checked=False, p=vfs_path: self._preview_mesh_vfs(p))
            menu.addAction(act)

        def _add_texture():
            act = QAction("Preview Texture", self)
            act.triggered.connect(lambda checked=False, p=vfs_path: self._preview_texture_vfs(p))
            menu.addAction(act)

        def _add_audio():
            act = QAction("Preview Audio", self)
            if audio_data:
                act.triggered.connect(lambda checked=False, d=audio_data: self._try_audio_preview(d))
            else:
                act.triggered.connect(lambda checked=False, p=vfs_path: self._preview_audio_vfs(p))
            menu.addAction(act)

        def _add_video():
            act = QAction("Preview Video", self)
            act.triggered.connect(lambda checked=False, p=vfs_path: self._preview_video_vfs(p))
            menu.addAction(act)

        if kind == "mesh":
            _add_mesh()
        elif kind == "texture":
            _add_texture()
        elif kind == "audio":
            _add_audio()
        elif kind == "video":
            _add_video()
        elif kind == "package":
            # If the package has been expanded, its children carry export_type
            # data. Show only the preview buttons matching what's actually
            # inside. If unexpanded (only the placeholder child exists), the
            # type cache may still know what's in the package — consult it
            # before falling back to offering all four.
            is_unexpanded = (
                item.childCount() == 0
                or (
                    item.childCount() == 1
                    and item.child(0).data(0, Qt.ItemDataRole.UserRole) == _PLACEHOLDER
                )
            )
            if is_unexpanded:
                cached_cats = (
                    self._type_cache.categories_for_package(vfs_path)
                    if self._type_cache is not None else set()
                )
                if not cached_cats:
                    _add_mesh()
                    _add_texture()
                    _add_audio()
                    _add_video()
                else:
                    if type_cache_mod.CATEGORY_MESH in cached_cats:
                        _add_mesh()
                    if type_cache_mod.CATEGORY_TEXTURE in cached_cats:
                        _add_texture()
                    if type_cache_mod.CATEGORY_AUDIO in cached_cats:
                        _add_audio()
                    if type_cache_mod.CATEGORY_VIDEO in cached_cats:
                        _add_video()
            else:
                child_kinds = {
                    self._classify_row(item.child(i))
                    for i in range(item.childCount())
                }
                if "mesh" in child_kinds:
                    _add_mesh()
                if "texture" in child_kinds:
                    _add_texture()
                if "audio" in child_kinds:
                    _add_audio()
                if "video" in child_kinds:
                    _add_video()

        # Properties always available — the CLI's get_props returns inline
        # JSON regardless of asset type, so this never has to disable.
        act_props = QAction("Preview Properties", self)
        act_props.triggered.connect(lambda checked=False, p=vfs_path: self._preview_props_ctx(p))
        menu.addAction(act_props)

        add_tree_expand_actions(menu, self._tree, item)

        menu.exec(global_pos)

    def _find_local_mesh(self, vfs_path: str) -> "Path | None":
        """Locate an exported PSK/PSKX for *vfs_path* if one is on disk.

        Mirrors `_find_local_file` but specifically targets the .psk → .uasset
        extension swap CUE4Parse performs on mesh exports.
        """
        if not self._export_output_dir:
            return None
        out = Path(self._export_output_dir)
        suffix = Path(vfs_path.lower()).suffix

        if suffix in self._MESH_EXTS:
            candidates = [out / vfs_path.lstrip("/")]
            name = vfs_path.rsplit("/", 1)[-1]
            candidates.append(out / name)
        else:
            stem_path = out / vfs_path.lstrip("/")
            name = vfs_path.rsplit("/", 1)[-1]
            candidates = []
            for ext in (".psk", ".pskx"):
                candidates.append(stem_path.with_suffix(ext))
                candidates.append((out / name).with_suffix(ext))

        for c in candidates:
            if c.is_file():
                return c
        return None

    def _find_local_file(self, vfs_path: str) -> "Path | None":
        """Try to locate an exported VFS file on disk.

        Handles the common case where the export changes the extension
        (e.g. .wem → .wav / .ogg, .uasset → .psk, .ubulk → .png).
        """
        if not self._export_output_dir:
            return None
        out = Path(self._export_output_dir)

        # Build candidate stems with possible alternate extensions
        audio_format = config.get("export_audio_format") or "wav"
        texture_format = config.get("export_texture_format") or "png"
        _ALT_EXT: dict[str, list[str]] = {
            ".wem": [f".{audio_format}", ".wav", ".ogg"],
            ".bnk": [f".{audio_format}", ".wav", ".ogg"],
        }

        # Collect candidate paths (original + alt extensions)
        candidates: list[Path] = []
        for base_path in [out / vfs_path.lstrip("/")]:
            candidates.append(base_path)
            ext = base_path.suffix.lower()
            for alt in _ALT_EXT.get(ext, []):
                candidates.append(base_path.with_suffix(alt))

        # Also try by filename only (flat export layout)
        name = vfs_path.rsplit("/", 1)[-1]
        for base_path in [out / name]:
            candidates.append(base_path)
            ext = base_path.suffix.lower()
            for alt in _ALT_EXT.get(ext, []):
                candidates.append(base_path.with_suffix(alt))

        for c in candidates:
            if c.is_file():
                return c
        return None

    # ------------------------------------------------------------------
    # Temp-export-for-preview
    # ------------------------------------------------------------------

    def _temp_dir_for_kind(self, kind: str) -> "Path | None":
        # Audio + video share the MediaPreviewerPanel's temp dir but live in
        # per-kind subdirs so the panel's per-list Clear button can rmtree
        # one half without nuking the other.
        if kind == "audio":
            return self._media_preview_temp_dir / "audio" if self._media_preview_temp_dir else None
        if kind == "video":
            return self._media_preview_temp_dir / "video" if self._media_preview_temp_dir else None
        if kind == "mesh":
            return self._mesh_preview_temp_dir
        if kind == "texture":
            return self._tga_preview_temp_dir
        return None

    def _candidate_extensions_for_kind(self, kind: str) -> tuple[str, ...]:
        """Output extensions the CLI may produce for a given preview kind."""
        if kind == "audio":
            audio_format = config.get("export_audio_format") or "wav"
            return (f".{audio_format}", ".wav", ".ogg")
        if kind == "mesh":
            return (".psk", ".pskx")
        if kind == "texture":
            tex_format = config.get("export_texture_format") or "png"
            return (f".{tex_format}", ".png", ".tga", ".dds")
        if kind == "video":
            # CLI preserves the source extension byte-for-byte for both
            # FileMediaSource and raw_video paths — matches RAW_VIDEO_EXTENSIONS.
            return (".mp4", ".webm", ".mov", ".bk2")
        return ()

    def _find_in_temp(self, temp_dir: Path, vfs_path: str, kind: str) -> "Path | None":
        """Look for an already-previewed copy of *vfs_path* in *temp_dir*.

        Mirrors `_find_local_file`'s candidate enumeration but rooted at the
        previewer's temp dir. Tries the VFS-mirrored path first, then the
        flat filename layout, and within each tries every output extension
        the CLI might have written for this kind.
        """
        candidates: list[Path] = []
        nested = temp_dir / vfs_path.lstrip("/")
        flat = temp_dir / vfs_path.rsplit("/", 1)[-1]
        for base in (nested, flat):
            candidates.append(base)
            for ext in self._candidate_extensions_for_kind(kind):
                candidates.append(base.with_suffix(ext))
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _predict_temp_output(self, temp_dir: Path, vfs_path: str, kind: str) -> Path:
        """Best guess at where the CLI will write the previewed file."""
        nested = temp_dir / vfs_path.lstrip("/")
        exts = self._candidate_extensions_for_kind(kind)
        if exts:
            return nested.with_suffix(exts[0])
        return nested

    def _kick_temp_export(self, vfs_path: str, kind: str):
        """Shared launcher for mesh / texture / audio temp-export-for-preview."""
        if self._exporting:
            self._status_label.setText("Export in progress — try again after it finishes")
            return
        if not self._mounted:
            self._status_label.setText("Mount an archive first")
            return
        temp_dir = self._temp_dir_for_kind(kind)
        if temp_dir is None:
            self._status_label.setText(f"{kind.capitalize()} preview temp dir not configured")
            return

        formats = {"mesh": False, "texture": False, "props": False,
                   "animation": False, "audio": False, "video": False}
        formats[kind] = True

        expected = self._predict_temp_output(Path(temp_dir), vfs_path, kind)
        self._pending_temp_preview = (str(expected), kind, vfs_path)

        name = vfs_path.rsplit("/", 1)[-1] or vfs_path
        self._status_label.setText(f"Exporting for preview: {name}")
        self._begin_export()
        self._unpacker.export(
            [vfs_path], str(temp_dir), formats=formats,
            texture_format=config.get("export_texture_format") or "png",
            audio_format=config.get("export_audio_format") or "wav",
        )

    def _preview_audio_vfs(self, vfs_path: str):
        """Preview a VFS audio file — use local export if available, else temp-export."""
        local = self._find_local_file(vfs_path)
        if local:
            self.media_preview.emit(str(local))
            return
        audio_temp = self._temp_dir_for_kind("audio")
        if audio_temp:
            cached = self._find_in_temp(audio_temp, vfs_path, "audio")
            if cached:
                self.media_preview.emit(str(cached))
                return
        self._kick_temp_export(vfs_path, "audio")

    def _preview_video_vfs(self, vfs_path: str):
        """Preview a VFS video file — local export if present, else temp-export.

        Two paths share this entry: a raw video leaf (``.bk2/.mp4/.webm/.mov``
        VFS path) goes through the CLI's ``export_video`` raw branch; a
        FileMediaSource UObject is exported via the same command and the CLI
        resolves the embedded ``FilePath`` reference internally.
        """
        local = self._find_local_file(vfs_path)
        if local:
            self.media_preview.emit(str(local))
            return
        video_temp = self._temp_dir_for_kind("video")
        if video_temp:
            cached = self._find_in_temp(video_temp, vfs_path, "video")
            if cached:
                self.media_preview.emit(str(cached))
                return
        self._kick_video_temp_export(vfs_path)

    def _kick_video_temp_export(self, vfs_path: str) -> None:
        """Send a video-only temp export via ``export_video`` and arm the dispatcher."""
        if self._exporting:
            self._status_label.setText("Export in progress — try again after it finishes")
            return
        if not self._mounted:
            self._status_label.setText("Mount an archive first")
            return
        temp_dir = self._temp_dir_for_kind("video")
        if temp_dir is None:
            self._status_label.setText("Video preview temp dir not configured")
            return

        # Decide whether this is a raw leaf or a FileMediaSource UObject by
        # extension — the CLI handler dispatches on the same `kind` field.
        from core.unpacker import RAW_VIDEO_EXTENSIONS
        suffix = Path(vfs_path.lower()).suffix
        leaf_kind = "raw_video" if suffix in RAW_VIDEO_EXTENSIONS else "file_media_source"

        # CLI writes to <temp_dir>/<basename> — predict the basename so the
        # dispatcher can find the file even when `succeeded` echoes the VFS
        # path rather than the disk output.
        name = vfs_path.rsplit("/", 1)[-1] or vfs_path
        expected = Path(temp_dir) / name
        self._pending_temp_preview = (str(expected), "video", vfs_path)

        self._status_label.setText(f"Exporting for preview: {name}")
        self._begin_export()
        self._unpacker.export_video(
            [{"vfs_path": vfs_path, "kind": leaf_kind}], str(temp_dir),
        )

    def _preview_mesh_vfs(self, vfs_path: str):
        """Preview a VFS mesh — local export if present, else temp-export."""
        local = self._find_local_mesh(vfs_path)
        if local:
            self.mesh_preview.emit(str(local))
            return
        if self._mesh_preview_temp_dir:
            cached = self._find_in_temp(self._mesh_preview_temp_dir, vfs_path, "mesh")
            if cached:
                self.mesh_preview.emit(str(cached))
                return
        self._kick_temp_export(vfs_path, "mesh")

    def _preview_texture_vfs(self, vfs_path: str):
        """Preview a VFS texture — local export if present, else temp-export."""
        local = self._find_local_file(vfs_path)
        if local:
            self.tga_preview.emit(str(local))
            return
        if self._tga_preview_temp_dir:
            cached = self._find_in_temp(self._tga_preview_temp_dir, vfs_path, "texture")
            if cached:
                self.tga_preview.emit(str(cached))
                return
        self._kick_temp_export(vfs_path, "texture")

    def _preview_props_ctx(self, vfs_path: str):
        """Load and display VFS file properties in the Text Viewer."""
        if self._exporting:
            self._status_label.setText("Export in progress — props will load when complete")
            return
        self._status_label.setText(f"Loading props for {vfs_path}...")
        self._unpacker.get_props(vfs_path)

    def _request_package_exports(self, item: QTreeWidgetItem, vfs_path: str) -> None:
        """Fetch a package's export list once; children land via exports_listed."""
        if item.data(0, Qt.ItemDataRole.UserRole + 3):
            return
        item.setData(0, Qt.ItemDataRole.UserRole + 3, True)  # mark as fetch-in-progress
        self._status_label.setText(f"Listing exports in {vfs_path}...")
        self._unpacker.list_exports(vfs_path)

    @Slot(QTreeWidgetItem, int)
    def _on_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Single click on a "+" package row lists its exports, matching the
        Asset Browser's single-click convention. Modified clicks (Ctrl/Shift
        multi-select) stay pure selection gestures."""
        if QApplication.keyboardModifiers() != Qt.KeyboardModifier.NoModifier:
            return
        if item.data(0, Qt.ItemDataRole.UserRole + 1):  # folder
            return
        vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if Path(vfs_path.lower()).suffix in self._PACKAGE_EXTS_FOR_MESH:
            self._request_package_exports(item, vfs_path)

    @Slot(QTreeWidgetItem, int)
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        is_folder = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if is_folder:
            return

        # Virtual WWise audio items → audio preview
        audio_data = item.data(0, Qt.ItemDataRole.UserRole + 2)
        if audio_data:
            self._try_audio_preview(audio_data)
            return

        vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if not vfs_path:
            return

        # Scanned FMOD banks carry their streams as real children — the
        # double-click expand toggle is the whole interaction.
        if vfs_path in self._fmod_banks:
            return

        # Package files (.upk, .uasset, .umap) → list exports as child nodes.
        # Normally handled by the single-click path; this covers keyboard
        # activation and is a no-op once the fetch flag is set.
        lower = vfs_path.lower()
        if Path(lower).suffix in self._PACKAGE_EXTS_FOR_MESH:
            self._request_package_exports(item, vfs_path)
            return

        # Regular audio files → audio preview (exported or temp)
        suffix = Path(lower).suffix
        if suffix in self._AUDIO_EXTS:
            self._preview_audio_vfs(vfs_path)
            return

        # Raw video leaves → video preview
        if suffix in self._VIDEO_EXTS:
            self._preview_video_vfs(vfs_path)
            return

        if self._exporting:
            self._status_label.setText("Export in progress \u2014 props will load when complete")
            return

        self._status_label.setText(f"Loading props for {vfs_path}...")
        self._unpacker.get_props(vfs_path)

    @Slot(str, list)
    def _on_exports_listed(self, path: str, exports: list, error: str = ""):
        """Populate child nodes under a package item with its exports."""
        item = self._find_tree_item(path)
        if item is None:
            # Path may have a sub-path structure — try matching by the raw VFS path stored in UserRole
            item = self._find_tree_item_by_data(path)
        if item is None:
            self._status_label.setText(f"Could not find tree node for {path}")
            return

        if not exports:
            # An empty list has two very different causes. Guessing
            # "unsupported format" when the CLI told us exactly why (missing
            # mappings, load timeout) is what sends people down wrong paths.
            if error:
                self._status_label.setText(f"Could not read {path}: {error}")
                self.log_message.emit(f"{path}: {error}", "error")
            else:
                self._status_label.setText(f"No exports found in {path}")
            return

        # Remove any existing children (placeholder or stale) before populating
        item.takeChildren()

        for exp in exports:
            name = exp.get("name", "?")
            export_type = exp.get("export_type", "Unknown")
            child = QTreeWidgetItem(item, [f"{name}  [{export_type}]"])
            # Store the parent package path so export can use it
            child.setData(0, Qt.ItemDataRole.UserRole, path)
            child.setData(0, Qt.ItemDataRole.UserRole + 1, False)  # not a folder
            # Stash the export_type so the right-click menu can pick the
            # correct "Preview …" action label without re-parsing display text.
            child.setData(0, Qt.ItemDataRole.UserRole + 4, export_type)
            child.setToolTip(0, f"Type: {export_type}\nPackage: {path}")
            child.setIcon(0, self._get_export_child_icon(export_type))

        # The "+" affordance has served its purpose — the branch arrow shows
        # expandability from here on.
        item.setIcon(0, self._get_package_open_icon())
        item.setExpanded(True)
        self._status_label.setText(f"{len(exports)} export(s) in {path}")

        # Re-apply filter only when one is active (same reasoning as _on_browse_result).
        if self._filter_is_active(
            self._search.text().strip().lower(),
            {k for k, cb in self._cat_checkboxes.items() if cb.isChecked()},
        ):
            self._filter_tree()

    def _find_tree_item_by_data(self, vfs_path: str) -> QTreeWidgetItem | None:
        """Search the tree for an item whose UserRole data matches *vfs_path*."""
        stack = [self._tree.invisibleRootItem()]
        while stack:
            parent = stack.pop()
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, Qt.ItemDataRole.UserRole) == vfs_path:
                    return child
                if child.childCount():
                    stack.append(child)
        return None

    def _try_audio_preview(self, audio_data: dict):
        """Resolve a virtual audio entry (WWise or FMOD) and emit media_preview."""
        if audio_data.get("bank_vfs_path"):
            self._try_fmod_preview(audio_data)
            return
        audio_format = config.get("export_audio_format") or "wav"
        evt_folder = audio_data.get("event_folder", "")
        full_folder = (
            (self._wwise_events_prefix.rstrip("/") + "/" + evt_folder).strip("/")
            if evt_folder
            else self._wwise_events_prefix.rstrip("/")
        )

        # Check already-exported location first
        if self._export_output_dir:
            local_path = Path(self._export_output_dir) / full_folder / f"{audio_data['debug_name']}.{audio_format}"
            if local_path.is_file():
                self.media_preview.emit(str(local_path))
                return

        # Check temp dir
        audio_temp = self._temp_dir_for_kind("audio")
        if audio_temp:
            temp_path = audio_temp / full_folder / f"{audio_data['debug_name']}.{audio_format}"
            if temp_path.is_file():
                self.media_preview.emit(str(temp_path))
                return

        # Export to temp dir for preview
        if self._exporting:
            self._status_label.setText("Export in progress \u2014 try again after it finishes")
            return
        if not self._mounted:
            self._status_label.setText("Mount an archive first")
            return
        if not audio_temp:
            self._status_label.setText("Audio not exported yet \u2014 export it first to preview")
            return

        temp_dir = str(audio_temp)
        entry = {
            "wem_vfs_path": audio_data["wem_vfs_path"],
            "target_name": audio_data["debug_name"],
            "target_folder": full_folder,
        }
        expected = audio_temp / full_folder / f"{audio_data['debug_name']}.{audio_format}"
        self._pending_temp_preview = (str(expected), "audio", audio_data["wem_vfs_path"])

        self._status_label.setText(f"Exporting for preview: {audio_data['debug_name']}")
        self._begin_export()
        self._unpacker.export_wwise_audio([entry], temp_dir, audio_format=audio_format)

    def _try_fmod_preview(self, audio_data: dict):
        """Resolve a virtual FMOD stream entry to a local file and emit media_preview.

        FMOD exports are always WAV — vgmstream decodes the bank subsong
        directly, so the export_audio_format setting doesn't apply here.
        """
        bank_path = audio_data["bank_vfs_path"]
        name = self._fmod_stream_name(bank_path, audio_data)
        disk_rel = self._fmod_disk_relpath(bank_path, audio_data)

        # Check already-exported location first
        if self._export_output_dir:
            local_path = Path(self._export_output_dir) / disk_rel
            if local_path.is_file():
                self.media_preview.emit(str(local_path))
                return

        # Check temp dir
        audio_temp = self._temp_dir_for_kind("audio")
        if audio_temp:
            temp_path = audio_temp / disk_rel
            if temp_path.is_file():
                self.media_preview.emit(str(temp_path))
                return

        # Export to temp dir for preview
        if self._exporting:
            self._status_label.setText("Export in progress — try again after it finishes")
            return
        if not self._mounted:
            self._status_label.setText("Mount an archive first")
            return
        if not audio_temp:
            self._status_label.setText("Audio not exported yet — export it first to preview")
            return

        entry = self._fmod_entry(bank_path, audio_data)
        expected = audio_temp / disk_rel
        self._pending_temp_preview = (str(expected), "audio", bank_path)

        self._status_label.setText(f"Exporting for preview: {name}")
        self._begin_export()
        self._unpacker.export_fmod_audio([entry], str(audio_temp))

    @Slot(str, list)
    def _on_props_result(self, path: str, exports: list):
        self._status_label.setText("Ready")
        text = json.dumps(exports, indent=2, ensure_ascii=False)
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        self.props_viewed.emit(f"Properties — {name}", text)

    @Slot(QTreeWidgetItem)
    def _on_item_expanded(self, item: QTreeWidgetItem):
        # User manually expanded — remove from user_collapsed so the filter
        # can auto-expand it again if needed.
        self._user_collapsed.discard(item)
        # Check if this node only has the placeholder → need to fetch children
        if item.childCount() == 1 and item.child(0).data(0, Qt.ItemDataRole.UserRole) == _PLACEHOLDER:
            vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            self._unpacker.browse(vfs_path)

    def _on_item_collapsed(self, item: QTreeWidgetItem):
        if self._programmatic_collapse:
            return
        # Any user collapse is remembered so the filter doesn't silently
        # re-open the folder on the next filter pass.
        self._auto_expanded.discard(item)
        self._user_collapsed.add(item)

    def _find_tree_item(self, vfs_path: str) -> QTreeWidgetItem | None:
        """Walk the tree to find the item matching *vfs_path*."""
        parts = [p for p in vfs_path.split("/") if p]
        parent = self._tree.invisibleRootItem()
        for part in parts:
            found = False
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.text(0) == part:
                    parent = child
                    found = True
                    break
            if not found:
                return None
        return parent

    @staticmethod
    def _make_path(parent_path: str, name: str) -> str:
        if parent_path:
            return f"{parent_path}/{name}"
        return name

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _get_formats(self) -> dict[str, bool]:
        return {
            "mesh": self._chk_mesh.isChecked(),
            "texture": self._chk_texture.isChecked(),
            "animation": self._chk_anim.isChecked(),
            "audio": self._chk_audio.isChecked(),
            "props": self._chk_props.isChecked(),
        }

    def _export_selected(self):
        selected = self._tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select one or more assets in the tree.")
            return

        output_dir = self._output_dir_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No Output", "Set an output directory first.")
            return

        config.set("unpack_output_dir", output_dir)
        self._export_output_dir = output_dir

        # Split selected items into regular VFS files and virtual audio items
        regular_paths = []
        wwise_entries = []
        fmod_entries = []
        seen_fmod_streams: set[tuple[str, int]] = set()

        def _add_fmod(bank_path: str, stream: dict) -> None:
            key = (bank_path, int(stream.get("index", 0) or 0))
            if key not in seen_fmod_streams:
                seen_fmod_streams.add(key)
                fmod_entries.append(self._fmod_entry(bank_path, stream))

        for item in selected:
            is_folder = item.data(0, Qt.ItemDataRole.UserRole + 1)
            vfs_path = item.data(0, Qt.ItemDataRole.UserRole)
            audio_data = item.data(0, Qt.ItemDataRole.UserRole + 2)
            if is_folder or not vfs_path:
                continue
            if audio_data:
                if audio_data.get("bank_vfs_path"):
                    _add_fmod(audio_data["bank_vfs_path"], audio_data)
                    continue
                evt_folder = audio_data.get("event_folder", "")
                full_folder = (self._wwise_events_prefix.rstrip("/") + "/" + evt_folder).strip("/") if evt_folder else self._wwise_events_prefix.rstrip("/")
                wwise_entries.append({
                    "wem_vfs_path": audio_data["wem_vfs_path"],
                    "target_name": audio_data["debug_name"],
                    "target_folder": full_folder,
                })
            elif vfs_path in self._fmod_banks:
                # A scanned bank row stands for all of its streams — decoded
                # WAVs are what the user is after, not the raw container.
                for stream in self._fmod_banks[vfs_path]:
                    _add_fmod(vfs_path, stream)
            else:
                regular_paths.append(vfs_path)

        if not regular_paths and not wwise_entries and not fmod_entries:
            QMessageBox.information(self, "No Files", "Selection contains only folders. Use 'Export Folder' instead.")
            return

        self._begin_export()

        # Chain: regular export first, then wwise, then fmod — each queued
        # stage is kicked off by _on_export_done when the previous completes.
        if regular_paths:
            if wwise_entries:
                self._pending_wwise_export = (wwise_entries, output_dir)
            if fmod_entries:
                self._pending_fmod_export = (fmod_entries, output_dir)
            self._unpacker.export(regular_paths, output_dir, self._get_formats(),
                                  texture_format=config.get("export_texture_format"),
                                  audio_format=config.get("export_audio_format"))
        elif wwise_entries:
            if fmod_entries:
                self._pending_fmod_export = (fmod_entries, output_dir)
            self._unpacker.export_wwise_audio(
                wwise_entries, output_dir,
                audio_format=config.get("export_audio_format"))
        else:
            self._unpacker.export_fmod_audio(fmod_entries, output_dir)

    def _export_folder(self):
        selected = self._tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select a folder in the tree.")
            return

        output_dir = self._output_dir_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No Output", "Set an output directory first.")
            return

        # Only operate on selected folder items; ignore non-folder rows so a
        # mixed selection doesn't silently drop items.
        folder_paths: list[str] = []
        for item in selected:
            is_folder = item.data(0, Qt.ItemDataRole.UserRole + 1)
            vfs_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            if is_folder and vfs_path:
                folder_paths.append(vfs_path)

        if not folder_paths:
            QMessageBox.information(
                self, "No Folders",
                "Selection contains no folders. Select one or more folder rows.",
            )
            return

        config.set("unpack_output_dir", output_dir)
        self._export_output_dir = output_dir

        self._begin_export()

        # Collect wwise audio entries if any selected folder is under Events.
        wwise_entries = []
        events_path = self._wwise_events_prefix.rstrip("/") if self._wwise_events_prefix else ""
        if self._wwise_scan_done and events_path:
            for vfs_path in folder_paths:
                if vfs_path == events_path or vfs_path.startswith(events_path + "/"):
                    rel_prefix = "" if vfs_path == events_path else vfs_path[len(events_path) + 1:]
                    for folder, entries in self._wwise_audio_map.items():
                        if rel_prefix == "" or folder == rel_prefix or folder.startswith(rel_prefix + "/"):
                            for audio in entries:
                                evt_folder = audio.get("event_folder", "")
                                full_folder = (events_path + "/" + evt_folder).strip("/") if evt_folder else events_path
                                wwise_entries.append({
                                    "wem_vfs_path": audio["wem_vfs_path"],
                                    "target_name": audio["debug_name"],
                                    "target_folder": full_folder,
                                })

        if wwise_entries:
            # Chain: send folder export first, queue wwise for after it completes
            self._pending_wwise_export = (wwise_entries, output_dir)

        # Collect FMOD stream entries for any scanned bank under a selected folder.
        fmod_entries = []
        if self._fmod_scan_done and self._fmod_banks:
            for vfs_path in folder_paths:
                prefix = vfs_path.rstrip("/") + "/"
                for bank_path, streams in self._fmod_banks.items():
                    if bank_path.startswith(prefix):
                        fmod_entries.extend(
                            self._fmod_entry(bank_path, s) for s in streams)
        if fmod_entries:
            self._pending_fmod_export = (fmod_entries, output_dir)

        # Export each selected folder. Only the *last* request will trigger
        # the chained WWise export; the CLI processes them sequentially.
        for vfs_path in folder_paths:
            self._unpacker.export_folder(
                vfs_path, output_dir, self._get_formats(),
                texture_format=config.get("export_texture_format"),
                audio_format=config.get("export_audio_format"),
            )

    def _begin_export(self):
        self._exporting = True
        self._export_btn.setEnabled(False)
        self._export_folder_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._handoff_btn.setEnabled(False)
        self._progress.setMaximum(0)
        self._progress.setValue(0)
        self._status_label.setText("Exporting...")

        # Snapshot existing PSKs (path → mtime) in the output dir so that,
        # after this export finishes, we can hand off ONLY the files this run
        # actually wrote — not every PSK that's accumulated in the folder over
        # prior sessions. Tracking mtime (not just path presence) means
        # re-exports of an existing file also register as "newly written."
        out_dir = self._export_output_dir or self._output_dir_edit.text().strip()
        if out_dir:
            self._pre_export_psks = self._snapshot_psks(Path(out_dir))
        else:
            self._pre_export_psks = {}

    @staticmethod
    def _scan_psks(folder: Path) -> list[Path]:
        """Return every .psk/.pskx under *folder*; tolerate missing folders."""
        if not folder.is_dir():
            return []
        return list(folder.rglob("*.psk")) + list(folder.rglob("*.pskx"))

    @classmethod
    def _snapshot_psks(cls, folder: Path) -> dict[Path, float]:
        """Return ``{resolved_path: mtime}`` for every PSK/PSKX in *folder*.

        Files that vanish or fail to stat between the rglob and the stat
        call are silently skipped — a transient race shouldn't blow up the
        snapshot.
        """
        snap: dict[Path, float] = {}
        for p in cls._scan_psks(folder):
            try:
                snap[p.resolve()] = p.stat().st_mtime
            except OSError:
                continue
        return snap

    def cancel_export(self):
        """Public entry point — used by MainWindow on profile switch / shutdown."""
        self._cancel_export()

    def _cancel_export(self):
        self._unpacker.cancel()
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("Cancelling...")

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, message: str):
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        self._status_label.setText(message)

    @Slot(list, list)
    def _on_export_done(self, succeeded: list, failed: list):
        # If there's a pending wwise audio export, send it now (chained)
        if self._pending_wwise_export:
            entries, out_dir = self._pending_wwise_export
            self._pending_wwise_export = None
            self._status_label.setText(f"Exporting {len(entries)} audio files...")
            self._unpacker.export_wwise_audio(
                entries, out_dir,
                audio_format=config.get("export_audio_format"))
            return

        # Then any pending FMOD stream export (chained after wwise/regular)
        if self._pending_fmod_export:
            entries, out_dir = self._pending_fmod_export
            self._pending_fmod_export = None
            self._status_label.setText(f"Exporting {len(entries)} FMOD streams...")
            self._unpacker.export_fmod_audio(entries, out_dir)
            return

        # Handle temp-export-for-preview (audio / mesh / texture)
        if self._pending_temp_preview:
            expected, kind, vfs_path = self._pending_temp_preview
            self._pending_temp_preview = None
            self._exporting = False
            self._export_btn.setEnabled(True)
            self._export_folder_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)

            signal_for_kind = {
                "audio": self.media_preview,
                "video": self.media_preview,
                "mesh": self.mesh_preview,
                "texture": self.tga_preview,
            }.get(kind)

            resolved: "Path | None" = None
            if Path(expected).is_file():
                resolved = Path(expected)
            elif succeeded:
                # CLI may have written under a slightly different path (e.g.
                # extension swap). Prefer a succeeded entry whose extension
                # matches the kind; otherwise take the first succeeded file.
                exts = {e.lower() for e in self._candidate_extensions_for_kind(kind)}
                for p in succeeded:
                    cand = Path(p)
                    if cand.is_file() and (not exts or cand.suffix.lower() in exts):
                        resolved = cand
                        break
                if resolved is None:
                    cand = Path(succeeded[0])
                    if cand.is_file():
                        resolved = cand

            # Final fallback: the CLI writes flat (basename only) but
            # `succeeded` echoes the VFS input path, not the disk output.
            # Rescan the temp dir for any candidate matching this vfs_path.
            if resolved is None:
                temp_dir = self._temp_dir_for_kind(kind)
                if temp_dir is not None:
                    resolved = self._find_in_temp(Path(temp_dir), vfs_path, kind)

            if resolved is not None and signal_for_kind is not None:
                self._status_label.setText("Ready")
                signal_for_kind.emit(str(resolved))
            elif signal_for_kind is None:
                self._status_label.setText(f"Preview kind {kind!r} has no signal")
            else:
                # Nothing landed on disk. "No data to preview" is only true when
                # the CLI exported cleanly and this asset simply had no payload;
                # if the export reported an error, that error is the real answer
                # and hiding it sends people hunting for the wrong problem.
                reason = next(
                    (f.get("error") for f in failed if f.get("error")), None)
                if reason:
                    self._status_label.setText(f"Cannot preview {kind}: {reason}")
                    self.log_message.emit(
                        f"Preview failed for {vfs_path}: {reason}", "error")
                else:
                    self._status_label.setText(
                        f"This asset has no {kind} data to preview"
                    )
            return

        self._exporting = False
        self._export_btn.setEnabled(True)
        self._export_folder_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.setMaximum(len(succeeded) + len(failed))
        self._progress.setValue(len(succeeded) + len(failed))

        msg = f"Export complete: {len(succeeded)} succeeded, {len(failed)} failed"
        self._status_label.setText(msg)
        self.log_message.emit(msg, "success" if not failed else "warning")

        if failed:
            details = "\n".join(f"  {f.get('path', '?')}: {f.get('error', '?')}" for f in failed[:20])
            self.log_message.emit(f"Failed exports:\n{details}", "error")
            self._show_export_failure_popup(failed)

        # Enable hand-off only for PSKs THIS export actually wrote. A file
        # counts as "newly written" if its path is missing from the snapshot
        # OR its mtime advanced since the snapshot — the second clause is what
        # makes re-exports of an already-existing file register correctly.
        if self._export_output_dir:
            current = self._snapshot_psks(Path(self._export_output_dir))
            new_psks: list[Path] = []
            for path, mtime in current.items():
                prev = self._pre_export_psks.get(path)
                if prev is None or mtime > prev:
                    new_psks.append(path)
            new_psks.sort()

            if new_psks:
                # Merge into the session list, deduping by resolved path so a
                # re-export over the same file doesn't grow the count.
                seen = {p.resolve() for p in self._session_handoff_psks}
                for p in new_psks:
                    if p not in seen:
                        self._session_handoff_psks.append(p)
                        seen.add(p)

            count = len(self._session_handoff_psks)
            if count:
                self._handoff_btn.setEnabled(True)
                self._handoff_btn.setText(f"Send {count} PSKs to Queue →")

    # ------------------------------------------------------------------
    # Hand-off to Asset Browser
    # ------------------------------------------------------------------

    def _handoff_psks(self):
        psk_files = list(self._session_handoff_psks)
        if not psk_files:
            QMessageBox.information(
                self, "No PSKs",
                "No newly-extracted PSKs to hand off. Export some assets from "
                "the tree first — the queue button will then track only what "
                "this session wrote, not everything already on disk.",
            )
            return
        self.psk_extracted.emit(psk_files)
        self.log_message.emit(
            f"Sent {len(psk_files)} PSK files to queue", "info",
        )
        # Clear so the next export batch starts fresh; reset the button to
        # match (disabled + default label until the next _on_export_done).
        self._session_handoff_psks = []
        self._handoff_btn.setEnabled(False)
        self._handoff_btn.setText("Send PSKs to Queue →")

    # ------------------------------------------------------------------
    # Export-section auto-expand
    # ------------------------------------------------------------------

    def _maybe_expand_export_section(self) -> None:
        """Open the bottom Export panel the first time the user selects a row.

        Once expanded we leave it alone; collapsing/auto-collapsing while the
        user is mid-action would feel jarring.
        """
        if not getattr(self, "_export_section", None):
            return
        if self._export_section._expanded:
            return
        if self._tree.selectedItems():
            self._export_section._toggle()

    # ------------------------------------------------------------------
    # AES keys
    # ------------------------------------------------------------------

    def _get_aes_keys(self) -> list[dict]:
        """Return the AES keys snapshotted from the active profile.

        The Unpacker no longer owns an editor for these — the profile dialog
        and the encrypted-archive prompt are the only edit points.
        """
        return list(self._aes_keys)

    def apply_profile_aes_keys(self, keys: list[dict]) -> None:
        """Replace the in-memory snapshot. MainWindow calls this after the
        AES prompt persists keys back to the profile so a subsequent remount
        sees the new values without a full profile reload."""
        self._aes_keys = list(keys or [])

    # ------------------------------------------------------------------
    # Browse helpers
    # ------------------------------------------------------------------

    def _browse_output_dir(self):
        start = self._output_dir_edit.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Select Output Directory", start)
        if path:
            self._output_dir_edit.setText(path)

    # ------------------------------------------------------------------
    # Error / warning / cleanup
    # ------------------------------------------------------------------

    def _show_export_failure_popup(self, failed: list, *, fatal_message: str | None = None) -> None:
        """Surface export failures as a modal so the user can't miss them.

        The user can tick "Don't show again..." to silence further popups for
        the rest of this CLI session — useful when a version mismatch produces
        the same error across hundreds of items in one queue.
        """
        if self._suppress_export_error_popup:
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        if fatal_message is not None:
            box.setWindowTitle("Export Failed")
            box.setText("The export was halted by a fatal error.")
            box.setInformativeText(fatal_message)
        else:
            n = len(failed)
            box.setWindowTitle("Export Errors")
            box.setText(f"{n} item{'s' if n != 1 else ''} failed to export.")
            box.setInformativeText("Click 'Show Details' for the per-item error list.")
            preview_n = 50
            lines = [
                f"{f.get('path', '?')}\n   -> {f.get('error', '?')}"
                for f in failed[:preview_n]
            ]
            if n > preview_n:
                lines.append(f"... and {n - preview_n} more.")
            box.setDetailedText("\n\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        chk = QCheckBox("Don't show this again until the CLI is re-mounted")
        box.setCheckBox(chk)
        box.exec()
        if chk.isChecked():
            self._suppress_export_error_popup = True

    @Slot(str)
    def _on_warning(self, message: str):
        self.log_message.emit(f"CUE4Parse warning: {message}", "warning")

    @Slot(str, str)
    def _on_version_warning(self, message: str, current_version: str):
        # Emit as a high-priority log line and surface a dismissible banner
        # near the log viewer so the user notices without needing to scroll.
        self.log_message.emit(f"Possible UE version mismatch: {message}", "error")
        banner = (
            f"Possible UE version mismatch — current: {current_version or '?'}. "
            f"{message}"
        )
        self.version_mismatch.emit(banner)

    @Slot(str, str, str)
    def _on_version_detected(self, suggested: str, source_exe: str, file_version: str):
        if suggested:
            # Auto-detected a version. The combo is read-only in the UI but we
            # still update it so the displayed value reflects what the CLI is
            # using. Saved via config so the next launch starts with the same.
            self._ue_version_combo.blockSignals(True)
            idx = self._ue_version_combo.findText(suggested)
            if idx >= 0:
                self._ue_version_combo.setCurrentIndex(idx)
            else:
                self._ue_version_combo.addItem(suggested)
                self._ue_version_combo.setCurrentText(suggested)
            self._ue_version_combo.blockSignals(False)
            log_msg = f"Auto-detected UE version: {suggested} (from {source_exe}, FileVersion {file_version})"
        else:
            # Detection failed
            log_msg = f"UE version detection failed: {file_version}"
        self.log_message.emit(log_msg, "info")

    @Slot(str)
    def _on_error(self, message: str):
        self._status_label.setText(f"Error: {message}")
        self._mount_btn.setEnabled(True)
        self._progress.setMaximum(1)
        self._progress.setValue(0)
        self.log_message.emit(f"CUE4Parse error: {message}", "error")
        if self._exporting:
            self._exporting = False
            self._export_btn.setEnabled(True)
            self._export_folder_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._show_export_failure_popup([], fatal_message=message)

    @Slot()
    def _on_process_ended(self):
        self._exporting = False
        self._mounted = False
        self._suppress_export_error_popup = False
        self._tree.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._export_folder_btn.setEnabled(False)
        self._mount_info.setText("")
        self._progress.setMaximum(1)
        self._progress.setValue(0)
        self._status_label.setText("CLI process ended — re-mount to continue")
        # Clear WWise state
        self._wwise_root = ""
        self._wwise_events_prefix = ""
        self._wwise_audio_map = {}
        self._wwise_scan_done = False
        self._pending_wwise_export = None
        # Clear FMOD state
        self._fmod_banks = {}
        self._fmod_scan_done = False
        self._pending_fmod_export = None

    def shutdown(self):
        """Stop the CLI process (called on app exit)."""
        self._unpacker.stop()
