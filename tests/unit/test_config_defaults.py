"""Tests for `config` module — defaults, get/set round-trip, presets loader."""

from __future__ import annotations

from pathlib import Path

import pytest

import config

pytestmark = pytest.mark.unit


REQUIRED_KEYS = {
    "game_folder",
    "blender_exe",
    "output_dir",
    "everything_dll",
    "psk_addon_name",
    "timeout_seconds",
    "presets_path",
    "cue4parse_cli",
    "unpack_output_dir",
    "aes_keys",
    "unpack_ue_version",
    "export_texture_format",
    "export_texture_webp_mode",
    "export_texture_webp_quality",
    "export_audio_format",
    "export_mesh_format",
    "unpacker_export_threads",
    "unpacker_gpu_accel",
    "unpacker_physical_memory_only",
    "unpacker_memory_limit_percent",
    "active_profile",
    "color_scheme",
    "custom_schemes",
    "ui_scale",
    "setup_complete",
}


def test_defaults_contain_required_keys():
    missing = REQUIRED_KEYS - set(config._DEFAULTS.keys())
    assert not missing, f"Missing default keys: {missing}"


def test_get_returns_default_when_unset(mock_qsettings):
    """An empty stub QSettings should fall back to the _DEFAULTS value."""
    assert config.get("psk_addon_name") == "bl_ext.blender_org.io_scene_psk_psa"
    assert config.get("export_texture_format") == "png"
    assert config.get("export_texture_webp_mode") == "lossless"
    assert config.get("export_mesh_format") == "psk"
    assert config.get("active_profile") == "Default"


def test_memory_policy_defaults_are_off(mock_qsettings):
    """The export memory policy ships disabled — no surprise caps."""
    raw = config.get("unpacker_physical_memory_only")
    assert raw in (False, "false", "False", "") or str(raw).lower() == "false"
    assert config.get_int("unpacker_memory_limit_percent") == 0


def test_get_returns_empty_string_for_unknown_key(mock_qsettings):
    assert config.get("totally_unknown_key_nope") == ""


def test_set_then_get_persists_value(mock_qsettings):
    config.set("game_folder", r"C:\Games\Test")
    assert config.get("game_folder") == r"C:\Games\Test"


def test_get_int_returns_default_when_unset(mock_qsettings):
    assert config.get_int("timeout_seconds") == 120


def test_get_int_round_trip(mock_qsettings):
    config.set("timeout_seconds", 300)
    assert config.get_int("timeout_seconds") == 300


def test_export_threads_default_is_auto(mock_qsettings):
    """0 means CLI-side auto sizing (one worker per CPU core)."""
    assert config.get_int("unpacker_export_threads") == 0


def test_addon_name_default_is_current_value():
    """Guards against accidental rewrite of the default addon name."""
    assert config._DEFAULTS["psk_addon_name"] == "bl_ext.blender_org.io_scene_psk_psa"


def test_gpu_accel_default_is_auto():
    "GPU texture decode defaults to auto (use GPU when present, CPU fallback)."
    assert config._DEFAULTS["unpacker_gpu_accel"] == "auto"


def test_get_presets_path_returns_path_object(mock_qsettings):
    p = config.get_presets_path()
    assert isinstance(p, Path)


def test_get_presets_path_falls_back_when_stored_path_missing(mock_qsettings):
    """Stale QSettings (e.g. old G:\\ Drive path) should heal to the bundled file."""
    config.set("presets_path", r"G:\Definitely\Does\Not\Exist\texture_presets.json")
    p = config.get_presets_path()
    bundled = config.base_dir() / "data" / "texture_presets.json"
    assert p == bundled


def test_get_presets_path_returns_stored_path_when_it_exists(mock_qsettings, tmp_path):
    """If the user-set presets path is a real file, hand it back unchanged."""
    real = tmp_path / "presets.json"
    real.write_text("{}", encoding="utf-8")
    config.set("presets_path", str(real))
    assert config.get_presets_path() == real


def test_load_presets_reads_real_file(real_presets):
    """Verify the real data/texture_presets.json shape (the source of truth)."""
    assert "presets" in real_presets
    assert "default_pbr" in real_presets["presets"]
    default = real_presets["presets"]["default_pbr"]
    assert "texture_slots" in default
    # Must have at least the canonical slots
    slots = default["texture_slots"]
    for required in ("base_color",):
        assert required in slots, f"Missing required slot: {required}"


def test_all_keys_returns_list_with_required_keys():
    keys = config.all_keys()
    assert isinstance(keys, list)
    assert REQUIRED_KEYS <= set(keys)


# ---------------------------------------------------------------------------
# Hardening: presets path safety + load fallback
# ---------------------------------------------------------------------------

def test_is_presets_path_safe_accepts_bundled_default():
    """The bundled data/texture_presets.json is the only path safe-by-default."""
    bundled = config.base_dir() / "data" / "texture_presets.json"
    assert config.is_presets_path_safe(bundled) is True


def test_is_presets_path_safe_rejects_external_path(tmp_path):
    other = tmp_path / "evil_presets.json"
    other.write_text("{}", encoding="utf-8")
    assert config.is_presets_path_safe(other) is False


def test_load_presets_falls_back_when_file_missing(monkeypatch, tmp_path, mock_qsettings):
    """A missing presets file must not raise — the bundled defaults take over."""
    config.set("presets_path", str(tmp_path / "does-not-exist.json"))
    out = config.load_presets()
    # Bundled file is what we should have read.
    assert "presets" in out
    assert "default_pbr" in out["presets"]


def test_load_presets_falls_back_on_invalid_json(monkeypatch, tmp_path, mock_qsettings):
    bad = tmp_path / "bad.json"
    bad.write_text("{not: json", encoding="utf-8")
    config.set("presets_path", str(bad))
    out = config.load_presets()
    assert "presets" in out


def test_load_presets_falls_back_on_shape_violation(monkeypatch, tmp_path, mock_qsettings):
    """A JSON file with the wrong shape must trigger fallback, not propagate."""
    weird = tmp_path / "weird.json"
    weird.write_text('{"not_presets": true}', encoding="utf-8")
    config.set("presets_path", str(weird))
    out = config.load_presets()
    # Bundled defaults always have "presets".
    assert "presets" in out


def test_validate_presets_shape_accepts_real_presets(real_presets):
    assert config._validate_presets_shape(real_presets) is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "string",
        {"presets": "not-a-dict"},
        {"presets": {"x": "not-a-dict"}},
        {"presets": {"x": {}}},  # missing texture_slots
        {"presets": {"x": {"texture_slots": "nope"}}},
    ],
)
def test_validate_presets_shape_rejects_bad_payloads(bad):
    assert config._validate_presets_shape(bad) is False
