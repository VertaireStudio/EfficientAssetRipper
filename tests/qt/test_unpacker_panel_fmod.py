"""Tests for the unpacker panel's FMOD bank scan, stream injection, and
preview/export routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from gui.unpacker_panel import UnpackerPanel

pytestmark = pytest.mark.qt


_SCAN_RESULT = {
    "type": "fmod_scan_result",
    "found": True,
    "total_banks": 2,
    "total_streams": 3,
    "banks": [
        {
            "bank_vfs_path": "Game/Content/FMOD/Desktop/SFX.bank",
            "bank_name": "SFX.bank",
            "streams": [
                {"index": 1, "name": "jump", "sample_rate": 48000, "duration_ms": 500},
                {"index": 2, "name": "", "sample_rate": 44100, "duration_ms": 1200},
            ],
        },
        {
            "bank_vfs_path": "Game/Content/FMOD/Desktop/Music.bank",
            "bank_name": "Music.bank",
            "streams": [
                {"index": 1, "name": "theme", "sample_rate": 48000, "duration_ms": 90_000},
            ],
        },
        # Metadata-only bank — no streams → must not enter the map
        {
            "bank_vfs_path": "Game/Content/FMOD/Desktop/Empty.bank",
            "bank_name": "Empty.bank",
            "streams": [],
        },
    ],
}


@pytest.fixture
def panel(qtbot):
    p = UnpackerPanel()
    qtbot.addWidget(p)
    return p


def test_wwise_scan_result_chains_fmod_scan(panel, monkeypatch):
    """The FMOD scan must only be sent after the wwise scan replies — the
    CLI's during-export command reader queues/drops commands sent earlier."""
    sent = []
    monkeypatch.setattr(panel._unpacker, "scan_fmod_banks",
                        lambda: sent.append("scan_fmod_banks"))
    panel._on_wwise_scan_result({"type": "wwise_scan_result", "found": False})
    assert sent == ["scan_fmod_banks"]

    # Once the fmod scan has completed, a later wwise result must not re-kick it.
    panel._fmod_scan_done = True
    panel._on_wwise_scan_result({"type": "wwise_scan_result", "found": False})
    assert sent == ["scan_fmod_banks"]


def test_fmod_disk_relpath_matches_cli_sanitization(panel):
    """CLI's SanitizeName maps spaces/invalid chars to '_' — the GUI's
    cache-path prediction must agree or every preview re-exports."""
    rel = panel._fmod_disk_relpath(
        "BLEAK/Content/FMOD/Desktop/Map Sounds.bank",
        {"index": 1, "name": "door-thump"})
    assert rel == Path("BLEAK") / "Content" / "FMOD" / "Desktop" / "Map_Sounds" / "0001_door-thump.wav"


def test_scan_result_builds_bank_map_and_skips_empty(panel):
    panel._on_fmod_scan_result(dict(_SCAN_RESULT))
    assert panel._fmod_scan_done
    assert set(panel._fmod_banks) == {
        "Game/Content/FMOD/Desktop/SFX.bank",
        "Game/Content/FMOD/Desktop/Music.bank",
    }


def test_scan_result_not_found_leaves_map_empty(panel):
    panel._on_fmod_scan_result({"type": "fmod_scan_result", "found": False})
    assert panel._fmod_scan_done
    assert panel._fmod_banks == {}


def test_stream_name_uses_fsb_name_with_index_prefix(panel):
    bank = "Game/Content/FMOD/Desktop/SFX.bank"
    assert panel._fmod_stream_name(bank, {"index": 1, "name": "jump"}) == "0001_jump"
    # Unnamed streams fall back to the bank stem
    assert panel._fmod_stream_name(bank, {"index": 2, "name": ""}) == "SFX_0002"


def test_fmod_bank_folder_strips_extension(panel):
    assert (panel._fmod_bank_folder("Game/Content/FMOD/Desktop/SFX.bank")
            == "Game/Content/FMOD/Desktop/SFX")


def test_browse_result_injects_stream_children_under_bank(panel):
    result = {
        "type": "fmod_scan_result",
        "found": True,
        "total_banks": 1,
        "total_streams": 2,
        "banks": [{
            "bank_vfs_path": "SFX.bank",
            "bank_name": "SFX.bank",
            "streams": [
                {"index": 1, "name": "jump", "sample_rate": 48000, "duration_ms": 500},
                {"index": 2, "name": "", "sample_rate": 44100, "duration_ms": 1200},
            ],
        }],
    }
    panel._on_fmod_scan_result(result)
    panel._on_browse_result("", [
        {"name": "SFX.bank", "is_folder": False, "asset_type": "FmodBank"},
    ])

    item = panel._tree.invisibleRootItem().child(0)
    assert item.text(0) == "SFX.bank"
    assert item.childCount() == 2

    first = item.child(0)
    assert first.text(0) == "0001_jump.wav"
    audio_data = first.data(0, Qt.ItemDataRole.UserRole + 2)
    assert audio_data["bank_vfs_path"] == "SFX.bank"
    assert audio_data["index"] == 1

    second = item.child(1)
    assert second.text(0) == "SFX_0002.wav"

    # Re-injection must not duplicate children
    panel._inject_fmod_streams(item)
    assert item.childCount() == 2


def test_scan_result_injects_into_already_loaded_tree(panel):
    # Bank row exists in the tree before the scan result lands.
    panel._on_browse_result("", [
        {"name": "SFX.bank", "is_folder": False, "asset_type": "FmodBank"},
    ])
    result = {
        "type": "fmod_scan_result",
        "found": True,
        "total_banks": 1,
        "total_streams": 1,
        "banks": [{
            "bank_vfs_path": "SFX.bank",
            "bank_name": "SFX.bank",
            "streams": [{"index": 1, "name": "jump", "sample_rate": 48000, "duration_ms": 500}],
        }],
    }
    panel._on_fmod_scan_result(result)
    item = panel._tree.invisibleRootItem().child(0)
    assert item.childCount() == 1
    assert item.text(0) == "SFX.bank"
    assert item.child(0).text(0) == "0001_jump.wav"


def test_try_audio_preview_routes_fmod_to_export_fmod_audio(panel, tmp_path, monkeypatch):
    panel._mounted = True
    panel._media_preview_temp_dir = tmp_path
    (tmp_path / "audio").mkdir()

    sent: list[tuple[list, str]] = []
    monkeypatch.setattr(panel._unpacker, "export_fmod_audio",
                        lambda entries, out_dir: sent.append((entries, out_dir)))

    audio_data = {"bank_vfs_path": "Game/FMOD/SFX.bank", "index": 3, "name": "boom"}
    panel._try_audio_preview(audio_data)

    assert len(sent) == 1
    entries, out_dir = sent[0]
    assert entries == [{
        "bank_vfs_path": "Game/FMOD/SFX.bank",
        "stream_index": 3,
        "target_name": "0003_boom",
        "target_folder": "Game/FMOD/SFX",
    }]
    assert Path(out_dir) == tmp_path / "audio"
    expected, kind, vfs = panel._pending_temp_preview
    assert kind == "audio"
    assert vfs == "Game/FMOD/SFX.bank"
    assert expected.endswith("0003_boom.wav")


def test_try_audio_preview_fmod_uses_cached_temp_file(panel, tmp_path, qtbot):
    panel._mounted = True
    panel._media_preview_temp_dir = tmp_path
    # "Map Sounds" → "Map_Sounds" on disk (CLI SanitizeName); the cache
    # check must look at the sanitized location.
    cached = tmp_path / "audio" / "Game" / "FMOD" / "Map_Sounds" / "0003_boom.wav"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"RIFF")

    audio_data = {"bank_vfs_path": "Game/FMOD/Map Sounds.bank", "index": 3, "name": "boom"}
    with qtbot.waitSignal(panel.media_preview, timeout=1000) as sig:
        panel._try_audio_preview(audio_data)
    assert Path(sig.args[0]) == cached


def test_export_done_chains_pending_fmod_export(panel, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(panel._unpacker, "export_fmod_audio",
                        lambda entries, out_dir, **kw: calls.append(
                            {"entries": entries, "out_dir": out_dir, **kw}))
    entries = [{"bank_vfs_path": "B.bank", "stream_index": 1,
                "target_name": "0001_x", "target_folder": "B"}]
    panel._pending_fmod_export = (entries, "C:/out")
    panel._on_export_done([], [])
    assert len(calls) == 1
    assert calls[0]["entries"] == entries
    assert calls[0]["out_dir"] == "C:/out"
    # The chained stage now passes threads=_export_threads(); the concrete
    # value depends on QSettings state (None = CLI auto), only pinned in the
    # dedicated threads tests.
    assert "threads" in calls[0]
    assert panel._pending_fmod_export is None


def test_export_selected_expands_bank_row_to_all_streams(panel, monkeypatch, tmp_path, mock_qsettings):
    panel._on_fmod_scan_result(dict(_SCAN_RESULT))
    panel._output_dir_edit.setText(str(tmp_path))

    sent: list[tuple[list, str]] = []
    monkeypatch.setattr(panel._unpacker, "export_fmod_audio",
                        lambda entries, out_dir, **kw: sent.append((entries, out_dir)))

    # Select the bank row itself (no audio_data) — should expand to streams.
    panel._on_browse_result("", [
        {"name": "SFX.bank", "is_folder": False, "asset_type": "FmodBank"},
    ])
    item = panel._tree.invisibleRootItem().child(0)
    item.setData(0, Qt.ItemDataRole.UserRole, "Game/Content/FMOD/Desktop/SFX.bank")
    item.setSelected(True)

    panel._export_selected()

    assert len(sent) == 1
    entries, out_dir = sent[0]
    assert out_dir == str(tmp_path)
    assert [e["stream_index"] for e in entries] == [1, 2]
    assert entries[0]["target_name"] == "0001_jump"
    assert entries[1]["target_name"] == "SFX_0002"
    assert entries[0]["target_folder"] == "Game/Content/FMOD/Desktop/SFX"


def test_double_click_on_scanned_bank_is_a_noop(panel, monkeypatch):
    panel._on_fmod_scan_result(dict(_SCAN_RESULT))
    panel._on_browse_result("", [
        {"name": "SFX.bank", "is_folder": False, "asset_type": "FmodBank"},
    ])
    item = panel._tree.invisibleRootItem().child(0)
    item.setData(0, Qt.ItemDataRole.UserRole, "Game/Content/FMOD/Desktop/SFX.bank")

    called = []
    monkeypatch.setattr(panel._unpacker, "get_props",
                        lambda path: called.append(path))
    monkeypatch.setattr(panel._unpacker, "list_exports",
                        lambda path: called.append(path))
    panel._on_item_double_clicked(item, 0)
    assert called == []
