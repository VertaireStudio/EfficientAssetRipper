"""Resilience tests for ``ProfileManager.load_profile``.

Cover the failure modes the GUI must keep working through:

- Truncated / malformed JSON  → quarantine + empty defaults
- Top-level non-dict JSON     → quarantine + empty defaults
- Permission / OS read errors → ``ProfileLoadError``
- Partial keys / unknown keys → defaults filled, unknowns retained
"""

from __future__ import annotations

import builtins
import json

import pytest

from core.profile_manager import ProfileLoadError, ProfileManager

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Corrupt-JSON recovery
# ---------------------------------------------------------------------------

def _write_raw(tmp_profiles_dir, name: str, raw: str) -> None:
    (tmp_profiles_dir / f"{name}.json").write_text(raw, encoding="utf-8")


def _list_quarantined(tmp_profiles_dir, name: str) -> list:
    return sorted(tmp_profiles_dir.glob(f"{name}.json.corrupt-*"))


def test_truncated_json_is_quarantined_and_recovers(tmp_profiles_dir):
    _write_raw(tmp_profiles_dir, "Truncated", '{"game_dir": "X:\\\\Foo"')  # missing closing brace

    pm = ProfileManager()
    loaded = pm.load_profile("Truncated")

    # Original file moved aside, NOT left in place to keep poisoning loads.
    assert not (tmp_profiles_dir / "Truncated.json").exists()
    backups = _list_quarantined(tmp_profiles_dir, "Truncated")
    assert len(backups) == 1
    # Quarantine kept the original bytes so the user can inspect them.
    assert "X:" in backups[0].read_text(encoding="utf-8")

    # Recovered profile is empty defaults with the requested name.
    assert loaded["name"] == "Truncated"
    assert loaded["game_dir"] == ""
    assert loaded["aes_keys"] == []


def test_malformed_json_is_quarantined(tmp_profiles_dir):
    _write_raw(tmp_profiles_dir, "Malformed", "{,,, not json ,,,}")
    pm = ProfileManager()
    loaded = pm.load_profile("Malformed")
    assert loaded["name"] == "Malformed"
    assert _list_quarantined(tmp_profiles_dir, "Malformed")


def test_top_level_list_is_quarantined(tmp_profiles_dir):
    _write_raw(tmp_profiles_dir, "WrongShapeList", "[1, 2, 3]")
    pm = ProfileManager()
    loaded = pm.load_profile("WrongShapeList")
    assert loaded["name"] == "WrongShapeList"
    assert loaded["aes_keys"] == []
    assert _list_quarantined(tmp_profiles_dir, "WrongShapeList")


def test_top_level_scalar_is_quarantined(tmp_profiles_dir):
    _write_raw(tmp_profiles_dir, "WrongShapeScalar", '"hello"')
    pm = ProfileManager()
    loaded = pm.load_profile("WrongShapeScalar")
    assert loaded["name"] == "WrongShapeScalar"
    assert _list_quarantined(tmp_profiles_dir, "WrongShapeScalar")


def test_top_level_null_is_quarantined(tmp_profiles_dir):
    _write_raw(tmp_profiles_dir, "WrongShapeNull", "null")
    pm = ProfileManager()
    loaded = pm.load_profile("WrongShapeNull")
    assert loaded["name"] == "WrongShapeNull"
    assert _list_quarantined(tmp_profiles_dir, "WrongShapeNull")


def test_repeat_corruption_does_not_clobber_first_backup(tmp_profiles_dir, monkeypatch):
    """Two corruptions in the same second still get distinct backups."""
    counter = {"n": 1000}

    def fake_time():
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr("core.profile_manager.time.time", fake_time)

    _write_raw(tmp_profiles_dir, "Repeat", "{not json")
    pm = ProfileManager()
    pm.load_profile("Repeat")

    _write_raw(tmp_profiles_dir, "Repeat", "{still not json")
    pm.load_profile("Repeat")

    backups = _list_quarantined(tmp_profiles_dir, "Repeat")
    assert len(backups) == 2


# ---------------------------------------------------------------------------
# mesh_processed rename migration
# ---------------------------------------------------------------------------

def test_legacy_psk_processed_migrates_to_mesh_processed(tmp_profiles_dir):
    """Profiles written before the Mesh Picker rename still restore history."""
    legacy = json.dumps({
        "name": "Legacy",
        "game_dir": "X:\\Foo",
        "psk_processed": ["F:/out/A.psk", "F:/out/B.glb"],
    })
    _write_raw(tmp_profiles_dir, "Legacy", legacy)

    pm = ProfileManager()
    loaded = pm.load_profile("Legacy")
    assert loaded["mesh_processed"] == ["F:/out/A.psk", "F:/out/B.glb"]
    assert "psk_processed" not in loaded


def test_psk_and_mesh_processed_union_merge(tmp_profiles_dir):
    """Both keys present → mesh_processed wins, legacy paths merged in."""
    raw = json.dumps({
        "name": "Mixed",
        "mesh_processed": ["F:/out/A.psk"],
        "psk_processed": ["F:/out/A.psk", "F:/out/C.fbx"],
    })
    _write_raw(tmp_profiles_dir, "Mixed", raw)

    pm = ProfileManager()
    loaded = pm.load_profile("Mixed")
    assert loaded["mesh_processed"] == ["F:/out/A.psk", "F:/out/C.fbx"]


def test_corrupt_processed_values_reset_to_list(tmp_profiles_dir):
    raw = json.dumps({
        "name": "Corrupt",
        "mesh_processed": "not-a-list",
        "psk_processed": 42,
    })
    _write_raw(tmp_profiles_dir, "Corrupt", raw)

    pm = ProfileManager()
    loaded = pm.load_profile("Corrupt")
    # mesh_processed reset to []; the legacy key (non-list) is discarded.
    assert loaded["mesh_processed"] == []


# ---------------------------------------------------------------------------
# OS-level errors propagate as ProfileLoadError
# ---------------------------------------------------------------------------

def test_permission_error_raises_profile_load_error(tmp_profiles_dir, monkeypatch):
    pm = ProfileManager()
    pm.create_profile("Locked", {"game_dir": "x"})

    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path).endswith("Locked.json") and (a and a[0] == "r" or kw.get("mode") == "r"):
            raise PermissionError("simulated lock")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("core.profile_manager.open", fake_open, raising=False)
    # ``profile_manager`` uses the bare ``open`` builtin; patch that too.
    monkeypatch.setattr("builtins.open", fake_open)

    with pytest.raises(ProfileLoadError) as exc:
        pm.load_profile("Locked")
    assert "Permission denied" in str(exc.value)


def test_os_error_raises_profile_load_error(tmp_profiles_dir, monkeypatch):
    pm = ProfileManager()
    pm.create_profile("IOFail", {"game_dir": "x"})

    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path).endswith("IOFail.json") and (a and a[0] == "r" or kw.get("mode") == "r"):
            raise OSError("simulated I/O error")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)

    with pytest.raises(ProfileLoadError) as exc:
        pm.load_profile("IOFail")
    assert "OS error" in str(exc.value)


# ---------------------------------------------------------------------------
# Partial / forward-compat schemas
# ---------------------------------------------------------------------------

def test_missing_keys_are_filled_with_defaults(tmp_profiles_dir):
    (tmp_profiles_dir / "Sparse.json").write_text(
        json.dumps({"game_dir": "X:\\Sparse"}),
        encoding="utf-8",
    )
    pm = ProfileManager()
    loaded = pm.load_profile("Sparse")
    assert loaded["game_dir"] == "X:\\Sparse"
    # Defaults filled in
    assert loaded["ue_version"] == "GAME_UE5_4"
    assert loaded["aes_keys"] == []
    assert loaded["auto_save_paths"] is False


def test_unknown_keys_are_preserved_for_forward_compat(tmp_profiles_dir):
    """A profile written by a newer client should round-trip cleanly."""
    (tmp_profiles_dir / "Future.json").write_text(
        json.dumps({
            "game_dir": "X",
            "future_feature": {"enabled": True, "level": 7},
        }),
        encoding="utf-8",
    )
    pm = ProfileManager()
    loaded = pm.load_profile("Future")
    assert loaded["future_feature"] == {"enabled": True, "level": 7}
