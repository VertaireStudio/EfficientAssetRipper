"""Integration tests for `AssetScanner.scan` end-to-end with FakeEverythingSDK."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.asset_scanner import AssetScanner
from tests.conftest import FakeEverythingSDK

pytestmark = pytest.mark.integration


def _write_mesh_props_for(tmp_path: Path, name: str, mat_name: str) -> Path:
    """Drop a JSON mesh props file in tmp_path and return its path."""
    p = tmp_path / f"{name}.props.txt"
    p.write_text(
        f"""
{{
  "Properties": {{
    "SkeletalMaterials": [
      {{
        "MaterialSlotName": "BodySlot",
        "Material": {{"ObjectPath": "/Game/X/{mat_name}.{mat_name}"}}
      }}
    ]
  }}
}}
""",
        encoding="utf-8",
    )
    return p


def _write_material_props(tmp_path: Path, name: str, tex_name: str) -> Path:
    p = tmp_path / f"{name}.props.txt"
    p.write_text(
        f"""
{{
  "Properties": {{
    "TextureParameterValues": [
      {{
        "ParameterInfo": {{"Name": "BaseColor"}},
        "ParameterValue": {{
          "ObjectPath": "/Game/X/Textures/{tex_name}.{tex_name}"
        }}
      }}
    ]
  }}
}}
""",
        encoding="utf-8",
    )
    return p


def test_scan_resolves_single_asset_end_to_end(tmp_path, fixtures_dir, tiny_presets):
    """A complete pipeline: PSK → mesh props → material props → texture file."""
    psk = tmp_path / "Game" / "Char" / "SK_Hero.psk"
    psk.parent.mkdir(parents=True)
    psk.write_bytes(b"\x00" * 32)  # any non-empty file works for the scanner

    mesh_props = _write_mesh_props_for(tmp_path, "SK_Hero", "MI_Hero_Body")
    mat_props = _write_material_props(tmp_path, "MI_Hero_Body", "T_Hero_C")
    tex_path = tmp_path / "Game" / "Char" / "Textures" / "T_Hero_C.tga"
    tex_path.parent.mkdir(parents=True)
    tex_path.write_bytes(b"\x00")

    sdk = FakeEverythingSDK(
        psk_files=[psk],
        props_files={
            "SK_Hero": [mesh_props],
            "MI_Hero_Body": [mat_props],
        },
        textures={"T_Hero_C": [tex_path]},
    )
    scanner = AssetScanner(str(tmp_path / "Game"), tiny_presets, sdk=sdk)
    results = scanner.scan()
    assert len(results) == 1
    a = results[0]
    assert a.name == "SK_Hero"
    assert a.mesh_props_found is True
    assert len(a.materials) == 1
    assert a.materials[0].material_name == "MI_Hero_Body"
    assert len(a.materials[0].textures) == 1
    assert a.status == "ready"


def test_scan_handles_missing_props_marks_no_props(tmp_path, tiny_presets):
    psk = tmp_path / "SK_Orphan.psk"
    psk.write_bytes(b"\x00" * 32)
    sdk = FakeEverythingSDK(psk_files=[psk], props_files={})
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)
    results = scanner.scan()
    assert len(results) == 1
    assert results[0].mesh_props_found is False
    assert results[0].status == "no_props"


def test_scan_falls_back_to_psk_binary_materials_when_props_empty(
    tmp_path, fixtures_dir, tiny_presets
):
    """If the mesh props lists no materials, the scanner reads MATT0000 from PSK."""
    # Use the synthetic minimal PSK with two MATT entries
    src = fixtures_dir / "psk" / "minimal.psk"
    psk = tmp_path / "MyChar.psk"
    psk.write_bytes(src.read_bytes())

    # Mesh props that lists no materials at all
    empty_mesh = tmp_path / "MyChar.props.txt"
    empty_mesh.write_text("{ \"Properties\": {} }", encoding="utf-8")

    sdk = FakeEverythingSDK(
        psk_files=[psk],
        props_files={"MyChar": [empty_mesh]},
    )
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)
    results = scanner.scan()
    assert len(results) == 1
    a = results[0]
    # Two binary mats from MATT0000
    mat_names = [m.material_name for m in a.materials]
    assert "M_TestBody" in mat_names
    assert "M_TestHelmet" in mat_names


def test_scan_reuses_seeded_cache_for_known_paths(tmp_path, tiny_presets):
    psk = tmp_path / "SK_Cached.psk"
    psk.write_bytes(b"\x00" * 32)
    sdk = FakeEverythingSDK(psk_files=[psk], props_files={})

    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)
    # Seed with a fully-resolved entry
    from core.asset_scanner import AssetEntry
    seeded = AssetEntry(
        psk_path=psk, name="SK_Cached", mesh_props_found=True
    )
    scanner.seed_cache([seeded])

    # Reset call count
    sdk.calls.clear()
    results = scanner.scan()
    assert len(results) == 1
    # find_props_file should NOT have been called because the entry was cached
    props_calls = [c for c in sdk.calls if c[0] == "find_props_file"]
    assert not props_calls, f"unexpected props lookup: {props_calls}"


def test_scan_progress_callback_invoked_with_monotonic_index(tmp_path, tiny_presets):
    psks = []
    for i in range(3):
        p = tmp_path / f"SK_P{i}.psk"
        p.write_bytes(b"\x00" * 32)
        psks.append(p)
    sdk = FakeEverythingSDK(psk_files=psks, props_files={})
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)

    seen: list[tuple[int, int, str]] = []
    scanner.scan(progress_callback=lambda c, t, m: seen.append((c, t, m)))

    indices = [c for c, _, _ in seen if isinstance(c, int)]
    # Indices should be monotonically non-decreasing
    assert all(a <= b for a, b in zip(indices, indices[1:])), indices


def test_scan_cancellation_breaks_loop(tmp_path, tiny_presets):
    psks = []
    for i in range(5):
        p = tmp_path / f"SK_C{i}.psk"
        p.write_bytes(b"\x00" * 32)
        psks.append(p)
    sdk = FakeEverythingSDK(psk_files=psks, props_files={})
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)

    def cb(cur, total, msg):
        if cur >= 2:
            scanner.cancel()

    results = scanner.scan(progress_callback=cb)
    # Cancellation should stop early; we shouldn't have all 5 resolved
    assert len(results) < 5


def test_scan_uses_find_mesh_files_backend(tmp_path, tiny_presets):
    """The scanner queries all mesh formats together, not just PSK."""
    mesh = tmp_path / "SK_Mix.glb"
    mesh.write_bytes(b"\x00" * 32)
    sdk = FakeEverythingSDK(psk_files=[mesh], props_files={})
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)
    scanner.scan()
    backend_calls = [c[0] for c in sdk.calls if "mesh" in c[0] or "psk" in c[0]]
    assert "find_mesh_files" in backend_calls
    assert "find_psk_files" not in backend_calls


def test_scan_resolves_glb_with_material_props(tmp_path, tiny_presets):
    """A .glb asset resolves through the same props-based material chain."""
    glb = tmp_path / "Game" / "Prop" / "SM_Crate.glb"
    glb.parent.mkdir(parents=True)
    glb.write_bytes(b"\x00" * 32)

    mesh_props = _write_mesh_props_for(tmp_path, "SM_Crate", "MI_Crate_Wood")
    mat_props = _write_material_props(tmp_path, "MI_Crate_Wood", "T_Crate_C")
    tex_path = tmp_path / "Game" / "Prop" / "Textures" / "T_Crate_C.tga"
    tex_path.parent.mkdir(parents=True)
    tex_path.write_bytes(b"\x00")

    sdk = FakeEverythingSDK(
        psk_files=[glb],
        props_files={
            "SM_Crate": [mesh_props],
            "MI_Crate_Wood": [mat_props],
        },
        textures={"T_Crate_C": [tex_path]},
    )
    scanner = AssetScanner(str(tmp_path / "Game"), tiny_presets, sdk=sdk)
    results = scanner.scan()
    assert len(results) == 1
    a = results[0]
    assert a.name == "SM_Crate"
    assert a.mesh_props_found is True
    assert a.materials[0].material_name == "MI_Crate_Wood"
    assert a.status == "ready"


def test_scan_glb_without_materials_is_no_materials_not_scan_failed(
    tmp_path, tiny_presets
):
    """GLB has no PSK chunks — the binary fallback must be skipped so an
    empty props material list surfaces as no_materials, not scan_failed."""
    glb = tmp_path / "SM_Bare.glb"
    glb.write_bytes(b"\x00" * 32)
    bare = tmp_path / "SM_Bare.props.txt"
    bare.write_text("{ \"Properties\": {} }", encoding="utf-8")

    sdk = FakeEverythingSDK(
        psk_files=[glb],
        props_files={"SM_Bare": [bare]},
    )
    scanner = AssetScanner(str(tmp_path), tiny_presets, sdk=sdk)
    results = scanner.scan()
    assert len(results) == 1
    a = results[0]
    # Props were found and parsed; the material list is simply empty.
    assert a.mesh_props_found is True
    assert a.materials == []
    assert a.status == "no_materials"
