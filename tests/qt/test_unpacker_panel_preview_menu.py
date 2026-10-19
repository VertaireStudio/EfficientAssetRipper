"""Tests for the unpacker panel's type-aware right-click preview menu and
the temp-export-for-preview dispatch in `_on_export_done`."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QMenu, QTreeWidgetItem

from core.type_cache import TypeCache
from gui.unpacker_panel import UnpackerPanel

import config

pytestmark = pytest.mark.qt


# The shared expand/collapse helper appends these to every tree menu — the
# preview-specific tests assert on the asset-specific actions only.
_EXPAND_ACTION_LABELS = {
    "Expand All",
    "Collapse All",
    "Expand Selected",
    "Collapse Selected",
}


def _capture_menu_actions(panel: UnpackerPanel, item: QTreeWidgetItem) -> list[str]:
    """Drive `_popup_context_menu` and return the list of asset-specific action labels.

    Filters out the trailing Expand/Collapse helper entries (and the
    separator that precedes them) so existing assertions stay focused on
    the preview-related options.
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


# ---------------------------------------------------------------------------
# _classify_row
# ---------------------------------------------------------------------------

def _make_row(panel: UnpackerPanel, vfs_path: str, *,
              is_folder: bool = False,
              audio_data: dict | None = None,
              export_type: str | None = None) -> QTreeWidgetItem:
    """Build a tree item with the same UserRole layout as the real panel."""
    item = QTreeWidgetItem(panel._tree)
    item.setData(0, Qt.ItemDataRole.UserRole, vfs_path)
    item.setData(0, Qt.ItemDataRole.UserRole + 1, is_folder)
    if audio_data is not None:
        item.setData(0, Qt.ItemDataRole.UserRole + 2, audio_data)
    if export_type is not None:
        item.setData(0, Qt.ItemDataRole.UserRole + 4, export_type)
    return item


def test_classify_row_export_type_mesh(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    for et in ("SkeletalMesh", "StaticMesh"):
        item = _make_row(p, "/Game/Foo.uasset", export_type=et)
        assert p._classify_row(item) == "mesh"


def test_classify_row_export_type_texture(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    for et in ("Texture2D", "TextureCube", "Texture2DArray"):
        item = _make_row(p, "/Game/Foo.uasset", export_type=et)
        assert p._classify_row(item) == "texture"


def test_classify_row_export_type_audio(qtbot):
    """Only SoundWave is previewable as audio. SoundCue is composite and
    AkAudioEvent is Wwise metadata — both fall through to "unknown" so the
    menu offers Preview Properties only."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset", export_type="SoundWave")
    assert p._classify_row(item) == "audio"
    for et in ("SoundCue", "AkAudioEvent"):
        item = _make_row(p, "/Game/Foo.uasset", export_type=et)
        assert p._classify_row(item) == "unknown", f"{et} should not be previewable"


def test_classify_row_unknown_export_type(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset", export_type="Material")
    assert p._classify_row(item) == "unknown"


def test_classify_row_wwise_audio_data_overrides_extension(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(
        p, "/Game/Foo.uasset",  # not an audio extension
        audio_data={"wem_vfs_path": "/x", "debug_name": "x"},
    )
    assert p._classify_row(item) == "audio"


def test_classify_row_extension_fallbacks(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    cases = [
        ("/Game/T.png", "texture"),
        ("/Game/T.tga", "texture"),
        ("/Game/T.dds", "texture"),
        ("/Game/M.psk", "mesh"),
        ("/Game/M.pskx", "mesh"),
        ("/Game/A.wav", "audio"),
        ("/Game/A.wem", "audio"),
        ("/Game/V.mp4", "video"),
        ("/Game/V.webm", "video"),
        ("/Game/V.mov", "video"),
        ("/Game/V.bk2", "video"),
        # Unexpanded packages classify as "package"; the context menu then
        # consults the type cache (when populated) to decide which preview
        # kinds to offer — see the menu tests below.
        ("/Game/X.uasset", "package"),
        ("/Game/X.upk", "package"),
        ("/Game/X.umap", "package"),
        ("/Game/X.weird", "unknown"),
    ]
    for vfs, expected in cases:
        item = _make_row(p, vfs)
        assert p._classify_row(item) == expected, f"{vfs} → {expected}"


def test_classify_row_export_type_video(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Movie.uasset", export_type="FileMediaSource")
    assert p._classify_row(item) == "video"


# ---------------------------------------------------------------------------
# _on_export_done dispatch (temp-export-for-preview)
# ---------------------------------------------------------------------------

def test_on_export_done_dispatches_mesh_kind(qtbot, tmp_path: Path):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._mesh_preview_temp_dir = tmp_path
    psk = tmp_path / "Game" / "Foo.psk"
    psk.parent.mkdir(parents=True, exist_ok=True)
    psk.write_bytes(b"")
    p._pending_temp_preview = (str(psk), "mesh", "/Game/Foo.uasset")
    p._exporting = True

    with qtbot.waitSignal(p.mesh_preview, timeout=2000) as blocker:
        p._on_export_done([str(psk)], [])
    assert blocker.args[0] == str(psk)
    assert p._pending_temp_preview is None
    assert p._exporting is False


def test_on_exports_listed_reports_cli_error_not_unsupported_guess(qtbot, mock_qsettings):
    """An empty listing with a CLI reason must show the reason, not a guess."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = QTreeWidgetItem(p._tree, ["Foo.uasset"])
    item.setData(0, Qt.ItemDataRole.UserRole, "/Game/Foo.uasset")

    reason = "a .usmap mappings file is required to read them"
    p._on_exports_listed("/Game/Foo.uasset", [], reason)

    status = p._status_label.text()
    assert reason in status
    assert "unsupported format" not in status


def test_on_exports_listed_without_error_stays_plain(qtbot, mock_qsettings):
    """No reason from the CLI means a plain 'no exports' message."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = QTreeWidgetItem(p._tree, ["Foo.uasset"])
    item.setData(0, Qt.ItemDataRole.UserRole, "/Game/Foo.uasset")

    p._on_exports_listed("/Game/Foo.uasset", [], "")

    assert p._status_label.text() == "No exports found in /Game/Foo.uasset"


def test_on_export_done_surfaces_export_error_instead_of_no_data(qtbot, tmp_path: Path):
    """A failed preview export must report the CLI's reason, not 'no data'."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._mesh_preview_temp_dir = tmp_path
    missing = tmp_path / "Game" / "Foo.psk"
    p._pending_temp_preview = (str(missing), "mesh", "/Game/Foo.uasset")
    p._exporting = True

    reason = "a .usmap mappings file is required to read them"
    p._on_export_done([], [{"path": "/Game/Foo.uasset", "error": reason}])

    status = p._status_label.text()
    assert reason in status
    assert "no mesh data" not in status
    assert p._pending_temp_preview is None
    assert p._exporting is False


def test_on_export_done_keeps_no_data_message_when_export_clean(qtbot, tmp_path: Path):
    """With no reported failure, the original 'no data' wording still applies."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._tga_preview_temp_dir = tmp_path
    missing = tmp_path / "Game" / "Foo.png"
    p._pending_temp_preview = (str(missing), "texture", "/Game/Foo.uasset")
    p._exporting = True

    p._on_export_done([], [])

    assert p._status_label.text() == "This asset has no texture data to preview"


def test_on_export_done_dispatches_texture_kind(qtbot, tmp_path: Path):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._tga_preview_temp_dir = tmp_path
    png = tmp_path / "Game" / "Foo.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"")
    p._pending_temp_preview = (str(png), "texture", "/Game/Foo.uasset")
    p._exporting = True

    with qtbot.waitSignal(p.tga_preview, timeout=2000) as blocker:
        p._on_export_done([str(png)], [])
    assert blocker.args[0] == str(png)


def test_on_export_done_dispatches_audio_kind(qtbot, tmp_path: Path):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    # Audio temp dir lives under <media_temp>/audio so per-kind Clear works.
    (tmp_path / "audio").mkdir(parents=True, exist_ok=True)
    p._media_preview_temp_dir = tmp_path
    wav = tmp_path / "audio" / "Game" / "Foo.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"")
    p._pending_temp_preview = (str(wav), "audio", "/Game/Foo.uasset")
    p._exporting = True

    with qtbot.waitSignal(p.media_preview, timeout=2000) as blocker:
        p._on_export_done([str(wav)], [])
    assert blocker.args[0] == str(wav)


def test_on_export_done_dispatches_video_kind(qtbot, tmp_path: Path):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    (tmp_path / "video").mkdir(parents=True, exist_ok=True)
    p._media_preview_temp_dir = tmp_path
    mp4 = tmp_path / "video" / "Foo.mp4"
    mp4.write_bytes(b"")
    p._pending_temp_preview = (str(mp4), "video", "/Game/Foo.uasset")
    p._exporting = True

    with qtbot.waitSignal(p.media_preview, timeout=2000) as blocker:
        p._on_export_done([str(mp4)], [])
    assert blocker.args[0] == str(mp4)


def test_on_export_done_extension_swap_fallback(qtbot, tmp_path: Path):
    """CLI may write a different extension than predicted (e.g. .tga not .png).
    The dispatcher should pick from `succeeded` matching the kind's extensions."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._tga_preview_temp_dir = tmp_path
    actual = tmp_path / "Game" / "Foo.tga"
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_bytes(b"")
    predicted = tmp_path / "Game" / "Foo.png"  # never created
    p._pending_temp_preview = (str(predicted), "texture", "/Game/Foo.uasset")
    p._exporting = True

    with qtbot.waitSignal(p.tga_preview, timeout=2000) as blocker:
        p._on_export_done([str(actual)], [])
    assert blocker.args[0] == str(actual)


# ---------------------------------------------------------------------------
# _kick_temp_export
# ---------------------------------------------------------------------------

def test_kick_temp_export_builds_correct_formats_dict(qtbot, tmp_path: Path, mock_qsettings, monkeypatch):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._mesh_preview_temp_dir = tmp_path
    p._mounted = True

    captured: dict = {}

    def fake_export(paths, output_dir, formats=None, texture_format="png", audio_format="wav",
                    mesh_format="psk", webp_mode="lossless", webp_quality=90,
                    threads=None, gpu=None,
                    memory_limit_percent=0, physical_only=False):
        captured.update({
            "paths": paths,
            "output_dir": output_dir,
            "formats": formats,
            "mesh_format": mesh_format,
        })

    monkeypatch.setattr(p._unpacker, "export", fake_export)

    p._kick_temp_export("/Game/Bar.uasset", "mesh")

    assert captured["paths"] == ["/Game/Bar.uasset"]
    assert captured["output_dir"] == str(tmp_path)
    assert captured["formats"] == {
        "mesh": True, "texture": False, "props": False,
        "animation": False, "audio": False, "video": False,
    }
    # Mesh format pass-through: config stores psk/glb/fbx, the CLI gets
    # psk/gltf (fbx exports psk here and converts afterwards).
    assert captured["mesh_format"] == {"psk": "psk", "glb": "gltf", "fbx": "psk"}.get(
        (config.get("export_mesh_format") or "psk").lower(), "psk")
    assert p._pending_temp_preview is not None
    expected_path, kind, vfs_path = p._pending_temp_preview
    assert kind == "mesh"
    assert Path(expected_path).suffix == ".psk"
    assert vfs_path == "/Game/Bar.uasset"


def test_kick_temp_export_refuses_when_not_mounted(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._mesh_preview_temp_dir = Path(".")
    p._mounted = False
    p._kick_temp_export("/Game/Foo.uasset", "mesh")
    assert p._pending_temp_preview is None
    assert "Mount" in p._status_label.text()


# ---------------------------------------------------------------------------
# Context menu construction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Menu rendering — what's actually in the popup
# ---------------------------------------------------------------------------

def test_menu_for_unexpanded_uasset_offers_all_preview_kinds(qtbot):
    """An unexpanded .uasset with no type cache could be anything; the menu
    must offer all four preview kinds so the user doesn't have to expand
    first. (Cached cases are exercised below.)"""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == [
        "Preview Mesh", "Preview Texture", "Preview Audio", "Preview Video",
        "Preview Properties",
    ]


def test_menu_for_loose_video_file(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Movies/Intro.bk2")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Video", "Preview Properties"]


def test_menu_for_expanded_file_media_source(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Movie.uasset", export_type="FileMediaSource")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Video", "Preview Properties"]


def test_menu_for_unexpanded_uasset_uses_type_cache_video_only(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._type_cache = TypeCache()
    p._type_cache.add_batch([{
        "path": "/Game/Movie.uasset",
        "exports": [{"name": "Intro", "export_type": "FileMediaSource"}],
    }])
    item = _make_row(p, "/Game/Movie.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Video", "Preview Properties"]


def _populate_type_cache(panel: UnpackerPanel, vfs_path: str, export_type: str) -> None:
    panel._type_cache = TypeCache()
    panel._type_cache.add_batch([{
        "path": vfs_path,
        "exports": [{"name": "X", "export_type": export_type}],
    }])


def test_menu_for_unexpanded_uasset_uses_type_cache_mesh_only(qtbot):
    """When the type cache knows the package is mesh-only, the unexpanded
    menu drops Texture/Audio."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    _populate_type_cache(p, "/Game/Foo.uasset", "SkeletalMesh")
    item = _make_row(p, "/Game/Foo.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Mesh", "Preview Properties"]


def test_menu_for_unexpanded_uasset_uses_type_cache_texture_only(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    _populate_type_cache(p, "/Game/Foo.uasset", "Texture2D")
    item = _make_row(p, "/Game/Foo.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Texture", "Preview Properties"]


def test_menu_for_unexpanded_uasset_uses_type_cache_soundwave(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    _populate_type_cache(p, "/Game/Foo.uasset", "SoundWave")
    item = _make_row(p, "/Game/Foo.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Audio", "Preview Properties"]


def test_menu_for_unexpanded_uasset_cached_as_soundcue_shows_only_props(qtbot):
    """SoundCue maps to CATEGORY_OTHER — none of the preview kinds match,
    so only Preview Properties is offered (no inert Preview Audio)."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    _populate_type_cache(p, "/Game/Foo.uasset", "SoundCue")
    item = _make_row(p, "/Game/Foo.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Properties"]


def test_menu_for_unexpanded_uasset_cached_as_akaudioevent_shows_only_props(qtbot):
    """AkAudioEvent uassets are Wwise metadata — preview the .wav next to
    them, not the uasset itself."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    _populate_type_cache(p, "/Game/Event.uasset", "AkAudioEvent")
    item = _make_row(p, "/Game/Event.uasset")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Properties"]


def test_menu_for_expanded_skeletal_mesh(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset", export_type="SkeletalMesh")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Mesh", "Preview Properties"]


def test_menu_for_expanded_texture2d(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset", export_type="Texture2D")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Texture", "Preview Properties"]


def test_menu_for_expanded_unknown_export_type_shows_only_props(qtbot):
    """An export type we don't know how to preview (e.g. Material) still
    shows Preview Properties so the user can inspect it."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Foo.uasset", export_type="Material")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Properties"]


def test_menu_for_loose_psk_file(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Mesh.psk")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Mesh", "Preview Properties"]


def test_menu_for_loose_image_file(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Tex.png")
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Texture", "Preview Properties"]


def test_menu_for_wwise_virtual_audio(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/audio.wem",
                     audio_data={"wem_vfs_path": "/x", "debug_name": "x"})
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Audio", "Preview Properties"]


def test_menu_skipped_for_folders(qtbot):
    """Folder rows produce no menu at all — the early return prevents any
    QMenu from being constructed."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_row(p, "/Game/Folder", is_folder=True)
    actions = _capture_menu_actions(p, item)
    assert actions == []


# ---------------------------------------------------------------------------
# Expanded .uasset menu — filter buttons by child types
# ---------------------------------------------------------------------------

def _make_package_with_children(panel: UnpackerPanel, vfs_path: str,
                                 child_export_types: list[str]) -> QTreeWidgetItem:
    """Build a .uasset package item with real children carrying export_type.
    Mirrors how `_on_exports_listed` populates the tree post-list_exports."""
    parent = _make_row(panel, vfs_path)
    for et in child_export_types:
        child = QTreeWidgetItem(parent)
        child.setData(0, Qt.ItemDataRole.UserRole, vfs_path)
        child.setData(0, Qt.ItemDataRole.UserRole + 1, False)
        child.setData(0, Qt.ItemDataRole.UserRole + 4, et)
    return parent


def test_menu_for_expanded_uasset_filters_by_mesh_child(qtbot):
    """An expanded mesh .uasset (StaticMesh + Material children) should offer
    only Preview Mesh — not Texture/Audio."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_package_with_children(
        p, "/Game/SM_Splitter_01.uasset",
        ["StaticMesh", "Material"],  # Material is non-previewable
    )
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Mesh", "Preview Properties"]


def test_menu_for_expanded_uasset_filters_by_texture_child(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_package_with_children(
        p, "/Game/TX_Conveyor_AO.uasset", ["Texture2D"],
    )
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Texture", "Preview Properties"]


def test_menu_for_expanded_uasset_no_previewable_children(qtbot):
    """A .uasset whose children are all non-previewable types still shows
    Preview Properties only — no inert preview buttons."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    item = _make_package_with_children(
        p, "/Game/MI_Foo.uasset", ["Material", "MaterialInstanceConstant"],
    )
    actions = _capture_menu_actions(p, item)
    assert actions == ["Preview Properties"]


# ---------------------------------------------------------------------------
# _on_export_done — flat-layout rescue (regression for "file not found" bug)
# ---------------------------------------------------------------------------

def test_on_export_done_finds_mesh_via_flat_rescue(qtbot, tmp_path: Path):
    """The CLI writes mesh exports flat (e.g. <temp>/MyMesh.psk) but
    `_predict_temp_output` builds a nested VFS-mirrored path and `succeeded`
    echoes the VFS input path, not the disk output. The rescan fallback via
    `_find_in_temp` should still locate the file on the *first* attempt."""
    p = UnpackerPanel()
    qtbot.addWidget(p)
    p._mesh_preview_temp_dir = tmp_path

    # Predicted (nested) path — doesn't exist on disk.
    predicted = tmp_path / "Game" / "Meshes" / "MyMesh.psk"
    # Actual (flat) path the CLI wrote.
    actual = tmp_path / "MyMesh.psk"
    actual.write_bytes(b"")

    p._pending_temp_preview = (str(predicted), "mesh", "/Game/Meshes/MyMesh.uasset")
    p._exporting = True

    # `succeeded` contains the VFS input path, not the disk output — exactly
    # what the CLI returns today and the source of the original bug.
    with qtbot.waitSignal(p.mesh_preview, timeout=2000) as blocker:
        p._on_export_done(["/Game/Meshes/MyMesh.uasset"], [])
    assert blocker.args[0] == str(actual)
    assert p._status_label.text() == "Ready"
