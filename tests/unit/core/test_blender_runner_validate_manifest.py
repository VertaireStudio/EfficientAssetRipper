"""Unit tests for `core.blender_runner._validate_manifest`."""

from __future__ import annotations

import os

import pytest

from core.blender_runner import ManifestValidationError, _validate_manifest

pytestmark = pytest.mark.unit


def _ok_manifest(tmp_path):
    """A minimal manifest that should validate cleanly."""
    return {
        "psk_path": "C:/games/foo/Mesh.psk",
        "output_path": str(tmp_path / "out.blend"),
        "materials": {},
    }


def test_validates_minimal_manifest(tmp_path):
    _validate_manifest(_ok_manifest(tmp_path))  # raises on failure


def test_rejects_missing_psk_path(tmp_path):
    m = _ok_manifest(tmp_path)
    m.pop("psk_path")
    with pytest.raises(ManifestValidationError, match="psk_path"):
        _validate_manifest(m)


def test_rejects_missing_output_path(tmp_path):
    m = _ok_manifest(tmp_path)
    m.pop("output_path")
    with pytest.raises(ManifestValidationError, match="output_path"):
        _validate_manifest(m)


def test_rejects_non_mesh_extension(tmp_path):
    m = _ok_manifest(tmp_path)
    m["psk_path"] = "C:/games/foo/Mesh.uasset"
    with pytest.raises(ManifestValidationError):
        _validate_manifest(m)


def test_accepts_pskx_extension(tmp_path):
    m = _ok_manifest(tmp_path)
    m["psk_path"] = "C:/games/foo/Mesh.pskx"
    _validate_manifest(m)


@pytest.mark.parametrize("ext", ["glb", "gltf", "fbx"])
def test_accepts_blender_native_mesh_inputs(tmp_path, ext):
    """Mesh Picker feeds GLB/GLTF/FBX exports through the same pipeline."""
    m = _ok_manifest(tmp_path)
    m["psk_path"] = f"C:/games/foo/Mesh.{ext}"
    _validate_manifest(m)


def test_rejects_relative_output_path(tmp_path):
    m = _ok_manifest(tmp_path)
    m["output_path"] = "out.blend"
    with pytest.raises(ManifestValidationError, match="absolute"):
        _validate_manifest(m)


def test_rejects_non_blend_output(tmp_path):
    m = _ok_manifest(tmp_path)
    m["output_path"] = str(tmp_path / "out.txt")
    with pytest.raises(ManifestValidationError, match=r"\.blend or \.fbx"):
        _validate_manifest(m)


def test_accepts_fbx_output_path(tmp_path):
    """Mesh format "fbx" manifests convert into .fbx — accepted output."""
    m = _ok_manifest(tmp_path)
    m["output_path"] = str(tmp_path / "out.fbx")
    _validate_manifest(m)


def test_rejects_blender_native_output_ext_typos(tmp_path):
    m = _ok_manifest(tmp_path)
    m["output_path"] = str(tmp_path / "out.gltf")
    with pytest.raises(ManifestValidationError, match=r"\.blend or \.fbx"):
        _validate_manifest(m)


def test_rejects_output_path_escaping_outputs_root(tmp_path):
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    m = _ok_manifest(tmp_path)
    m["outputs_root"] = str(safe_root)
    # Pick a sibling so the commonpath doesn't match the safe_root.
    other = tmp_path / "other"
    other.mkdir()
    m["output_path"] = str(other / "out.blend")
    with pytest.raises(ManifestValidationError, match="escapes"):
        _validate_manifest(m)


def test_accepts_output_path_inside_outputs_root(tmp_path):
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    m = _ok_manifest(tmp_path)
    m["outputs_root"] = str(safe_root)
    m["output_path"] = str(safe_root / "sub" / "out.blend")
    _validate_manifest(m)


def test_rejects_non_image_texture_extension(tmp_path):
    m = _ok_manifest(tmp_path)
    m["materials"] = {
        "MyMat": {
            "textures": {
                "BaseColor": {
                    "path": "C:/games/foo/T_evil.exe",
                }
            }
        }
    }
    with pytest.raises(ManifestValidationError, match="extension"):
        _validate_manifest(m)


def test_accepts_recognised_image_extensions(tmp_path):
    m = _ok_manifest(tmp_path)
    m["materials"] = {
        "MyMat": {
            "textures": {
                "BaseColor": {"path": "C:/games/foo/T_basecolor.png"},
                "Normal": {"path": "C:/games/foo/T_normal.tga"},
                "ORM": {"path": "C:/games/foo/T_orm.exr"},
            }
        }
    }
    _validate_manifest(m)


def test_rejects_missing_texture_path(tmp_path):
    m = _ok_manifest(tmp_path)
    m["materials"] = {
        "MyMat": {
            "textures": {
                "BaseColor": {},
            }
        }
    }
    with pytest.raises(ManifestValidationError, match="path"):
        _validate_manifest(m)


def test_rejects_non_dict_materials(tmp_path):
    m = _ok_manifest(tmp_path)
    m["materials"] = []
    with pytest.raises(ManifestValidationError, match="materials"):
        _validate_manifest(m)


# ---------------------------------------------------------------------------
# mode: "preview_arrays" (GLB/FBX → npz geometry for the in-app previewer)
# ---------------------------------------------------------------------------

def _preview_manifest(tmp_path, src_name="in.glb"):
    src = tmp_path / src_name
    src.write_bytes(b"\x00")
    return {
        "mode": "preview_arrays",
        "psk_path": str(src),
        "output_path": str(tmp_path / "out.npz"),
    }


def test_preview_mode_validates_glb_to_npz(tmp_path):
    _validate_manifest(_preview_manifest(tmp_path))


def test_preview_mode_rejects_psk_input(tmp_path):
    m = _preview_manifest(tmp_path, "in.psk")
    with pytest.raises(ManifestValidationError, match="preview input"):
        _validate_manifest(m)


def test_preview_mode_rejects_bad_output_extension(tmp_path):
    m = _preview_manifest(tmp_path)
    m["output_path"] = str(tmp_path / "out.blend")
    with pytest.raises(ManifestValidationError, match=r"\.npz"):
        _validate_manifest(m)


def test_preview_mode_requires_existing_source(tmp_path):
    m = _preview_manifest(tmp_path)
    os.unlink(m["psk_path"])
    with pytest.raises(ManifestValidationError, match="does not exist"):
        _validate_manifest(m)


def test_preview_mode_rejects_relative_output(tmp_path):
    m = _preview_manifest(tmp_path)
    m["output_path"] = "out.npz"
    with pytest.raises(ManifestValidationError, match="absolute"):
        _validate_manifest(m)


def test_preview_mode_respects_outputs_root(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    m = _preview_manifest(tmp_path)
    m["outputs_root"] = str(safe)
    with pytest.raises(ManifestValidationError, match="escapes"):
        _validate_manifest(m)


def test_standard_mode_ignores_mode_dispatch(tmp_path):
    """Standard manifests (no mode key) still validate as before."""
    _validate_manifest(_ok_manifest(tmp_path))
