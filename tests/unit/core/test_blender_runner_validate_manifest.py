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


def test_rejects_non_psk_extension(tmp_path):
    m = _ok_manifest(tmp_path)
    m["psk_path"] = "C:/games/foo/Mesh.fbx"
    with pytest.raises(ManifestValidationError, match=r"\.psk"):
        _validate_manifest(m)


def test_accepts_pskx_extension(tmp_path):
    m = _ok_manifest(tmp_path)
    m["psk_path"] = "C:/games/foo/Mesh.pskx"
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
