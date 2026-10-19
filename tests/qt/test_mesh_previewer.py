"""Qt-tier tests for `gui.mesh_previewer.MeshPreviewerPanel`.

Avoids touching real GL state — we never trigger paintGL or showEvent. The
viewport widget is constructed but never shown, so its OpenGL context is
never created and the shader / VBO code paths stay dormant.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

import gui.mesh_previewer as gui_mesh_previewer
from gui.mesh_previewer import MeshPreviewerPanel, _MeshGLView

pytestmark = pytest.mark.qt


# Reuse the synthetic-PSK builder verbatim so this test stays self-contained.
def _chunk_header(chunk_id: str, type_flag: int, data_size: int, data_count: int) -> bytes:
    return chunk_id.encode("ascii").ljust(20, b"\x00") + struct.pack(
        "<III", type_flag, data_size, data_count
    )


def _tiny_psk_bytes() -> bytes:
    out = bytearray()
    out += _chunk_header("ACTRHEAD", 0, 0, 0)
    out += _chunk_header("PNTS0000", 0, 12, 3)
    for x, y, z in [(0, 0, 0), (1, 0, 0), (0, 1, 0)]:
        out += struct.pack("<fff", x, y, z)
    out += _chunk_header("VTXW0000", 0, 16, 3)
    for pi, u, v in [(0, 0.0, 0.0), (1, 1.0, 0.0), (2, 0.0, 1.0)]:
        out += struct.pack("<I", pi) + struct.pack("<ff", u, v) + b"\x00" * 4
    out += _chunk_header("FACE0000", 0, 12, 1)
    out += struct.pack("<HHH", 0, 1, 2) + b"\x00" * 6
    return bytes(out)


def test_mesh_previewer_constructs_empty(qtbot):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)
    assert p._gl is not None
    # Default mode is "flat"; mode toggle buttons reflect it.
    assert p._gl.render_mode == "flat"
    assert p._btn_flat.isChecked()
    assert not p._btn_uv.isChecked()
    assert not p._btn_wire.isChecked()


def test_mesh_previewer_mode_buttons_toggle_state(qtbot):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    p._gl.set_render_mode("uv")
    assert p._gl.render_mode == "uv"
    assert p._btn_uv.isChecked()
    assert not p._btn_flat.isChecked()

    p._gl.set_render_mode("wire")
    assert p._gl.render_mode == "wire"
    assert p._btn_wire.isChecked()


def test_mesh_previewer_load_missing_file_shows_placeholder(qtbot, tmp_path: Path):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    p.load_psk(str(tmp_path / "does_not_exist.psk"))
    assert p._stack.currentWidget() is p._placeholder
    assert "Not found" in p._status_name.text() or "not found" in p._status_name.text()


def test_mesh_previewer_load_valid_psk_emits_mesh_loaded(qtbot, tmp_path: Path):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    psk = tmp_path / "tri.psk"
    psk.write_bytes(_tiny_psk_bytes())

    # The loader runs on QThreadPool — wait for the GL view's mesh_loaded
    # signal to fire (it fires once the mesh data arrives, even if the GL
    # context never initialises because the widget isn't shown).
    with qtbot.waitSignal(p._gl.mesh_loaded, timeout=4000) as blocker:
        p.load_psk(str(psk))
    vert_count, tri_count = blocker.args
    assert tri_count == 1
    assert vert_count == 3
    # Status strip reflects the loaded mesh.
    assert "verts" in p._status_stats.text()
    assert "tris" in p._status_stats.text()


def test_mesh_previewer_clear_resets_to_placeholder(qtbot, tmp_path: Path):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    psk = tmp_path / "tri.psk"
    psk.write_bytes(_tiny_psk_bytes())
    with qtbot.waitSignal(p._gl.mesh_loaded, timeout=4000):
        p.load_psk(str(psk))

    p.clear()
    assert p._stack.currentWidget() is p._placeholder
    assert p._status_name.text() == ""


def test_meshglview_orbit_camera_unlimited(qtbot):
    """Quaternion orbit must support full 360° rotation in any direction —
    no clamping, no degenerate state when crossing the poles."""
    import numpy as np
    from gui.mesh_previewer import (
        _quat_axis_angle,
        _quat_mul,
        _quat_normalize,
    )

    v = _MeshGLView()
    qtbot.addWidget(v)

    # Spam pitch all the way past the pole and back. Without clamping and
    # with quaternion accumulation, orientation must stay normalized.
    for _ in range(2000):
        pitch = _quat_axis_angle((1.0, 0.0, 0.0), 0.05)
        v._orientation = _quat_normalize(_quat_mul(v._orientation, pitch))

    norm = float(np.linalg.norm(v._orientation))
    assert abs(norm - 1.0) < 1e-6, f"quaternion drifted: |q|={norm}"

    # And spam yaw the same way — the camera must accept any total angle.
    for _ in range(2000):
        yaw = _quat_axis_angle((0.0, 1.0, 0.0), 0.05)
        v._orientation = _quat_normalize(_quat_mul(yaw, v._orientation))

    norm = float(np.linalg.norm(v._orientation))
    assert abs(norm - 1.0) < 1e-6


def test_meshglview_default_orientation_is_unit(qtbot):
    import numpy as np
    v = _MeshGLView()
    qtbot.addWidget(v)
    assert abs(float(np.linalg.norm(v._orientation)) - 1.0) < 1e-6


def test_mesh_previewer_temp_dir_exists(qtbot):
    """The unpacker drops preview-only PSKs here, so it must exist."""
    from pathlib import Path
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)
    assert isinstance(p.temp_dir, Path)
    assert p.temp_dir.is_dir()
    assert p.temp_dir.name.startswith("ear_mesh_preview_")


def test_meshglview_reset_view_restores_default_orientation(qtbot):
    import numpy as np
    from gui.mesh_previewer import _quat_axis_angle

    v = _MeshGLView()
    qtbot.addWidget(v)
    default = v._orientation.copy()
    v._orientation = _quat_axis_angle((1.0, 0.0, 0.0), 1.234)
    assert not np.allclose(v._orientation, default)

    v.reset_view()
    np.testing.assert_allclose(v._orientation, default, atol=1e-9)


# ---------------------------------------------------------------------------
# GLB/FBX conversion path (headless Blender → .npz → PskMesh)
# ---------------------------------------------------------------------------

def _write_preview_npz(path: Path) -> None:
    """Geometry arrays in the exact layout preview_convert.py writes."""
    verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    normals = np.zeros((3, 3), dtype=np.float32)
    uv0 = np.zeros((3, 2), dtype=np.float32)
    faces = np.array([[0, 1, 2]], dtype=np.uint32)
    np.savez(path, verts=verts, normals=normals, uv0=uv0, faces=faces)


def test_load_mesh_unsupported_extension_shows_placeholder(qtbot, tmp_path: Path):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)
    doc = tmp_path / "model.usdz"
    doc.write_bytes(b"\x00")
    p.load_mesh(str(doc))
    assert p._stack.currentWidget() is p._placeholder
    assert "Unsupported" in p._status_name.text()


def test_load_mesh_glb_without_blender_never_spawns_worker(
    qtbot, tmp_path: Path, monkeypatch
):
    """No Blender configured → friendly placeholder, no dangling worker."""
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)
    monkeypatch.setattr("config.get", lambda key, *a, **k: "")

    glb = tmp_path / "model.glb"
    glb.write_bytes(b"glTF")
    p.load_mesh(str(glb))
    assert p._convert_worker is None
    assert p._stack.currentWidget() is p._placeholder
    assert "Blender" in p._placeholder.text()


def test_load_mesh_glb_with_blender_spawns_conversion_worker(
    qtbot, tmp_path: Path, monkeypatch
):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)
    worker_holder: dict = {}
    worker_cls = gui_mesh_previewer._PreviewConvertWorker

    class _StubWorker(worker_cls):
        def start(self):
            worker_holder["spawned"] = True

    monkeypatch.setattr(gui_mesh_previewer, "_PreviewConvertWorker", _StubWorker)
    monkeypatch.setattr(
        "config.get",
        lambda key, *a, **k: "C:/fake/blender.exe" if key == "blender_exe" else None,
    )

    glb = tmp_path / "model.glb"
    glb.write_bytes(b"glTF")
    p.load_mesh(str(glb))
    assert worker_holder.get("spawned") is True
    assert p._convert_worker is not None
    assert "Converting" in p._status_name.text()


def test_on_convert_done_builds_mesh_and_unlinks_npz(qtbot, tmp_path: Path):
    from core.psk_reader import PskMesh
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    npz = p.temp_dir / "preview_model.npz"
    _write_preview_npz(npz)
    p._current_path = str(tmp_path / "model.glb")
    token = p._load_token + 1
    p._load_token = token

    with qtbot.waitSignal(p._gl.mesh_loaded, timeout=2000):
        p._on_convert_done(token, str(npz))

    assert p._stack.currentWidget() is p._gl
    mesh = p._gl._mesh
    assert isinstance(mesh, PskMesh)
    assert mesh.vert_count == 3
    assert mesh.tri_count == 1
    # One-shot temp file: deleted as soon as the mesh is built in memory.
    assert not npz.exists()


def test_on_convert_done_with_corrupt_npz_fails_cleanly(qtbot, tmp_path: Path):
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    npz = p.temp_dir / "preview_bad.npz"
    npz.write_bytes(b"not a real npz file")
    p._current_path = str(tmp_path / "model.glb")
    token = p._load_token + 1
    p._load_token = token

    with qtbot.waitSignal(p.log_message, timeout=2000):
        p._on_convert_done(token, str(npz))

    assert p._stack.currentWidget() is p._placeholder
    assert "Failed" in p._status_name.text()
    # Corrupt conversion output is removed so it can't be reused or pile up.
    assert not npz.exists()


def test_on_convert_done_stale_token_ignored(qtbot, tmp_path: Path):
    """A conversion token older than the current load must not render."""
    p = MeshPreviewerPanel()
    qtbot.addWidget(p)

    npz = p.temp_dir / "preview_stale.npz"
    _write_preview_npz(npz)
    stale_token = p._load_token
    p._load_token += 1  # a newer load superseded this conversion
    p._on_convert_done(stale_token, str(npz))
    assert p._stack.currentWidget() is p._placeholder
    # Stale npz stays for cleanup at exit rather than trashing the live view.
    assert npz.exists()


class _FakeBlenderResult:
    """Minimal stand-in for core.blender_runner.BlenderResult."""
    def __init__(self, success: bool = True):
        self.success = success
        self.error_message = "stubbed failure" if not success else ""


def test_convert_worker_manifest_uses_preview_mode(monkeypatch, tmp_path: Path):
    """The converter must tag its manifest with mode=preview_arrays — the
    runner otherwise validates .npz against the standard .blend/.fbx
    allowlist and fails before Blender even starts (regression)."""
    captured: list[dict] = []

    monkeypatch.setattr(
        gui_mesh_previewer, "run_blender",
        lambda **kw: (captured.append(kw["manifest"]), _FakeBlenderResult())[1],
    )
    monkeypatch.setattr(
        "config.get",
        lambda key, *a, **k: "C:/fake/blender.exe" if key == "blender_exe" else None,
    )

    src = tmp_path / "model.glb"
    src.write_bytes(b"glTF")
    out = tmp_path / "model.npz"
    np.savez(out,
             verts=np.zeros((0, 3), dtype=np.float32),
             normals=np.zeros((0, 3), dtype=np.float32),
             uv0=np.zeros((0, 2), dtype=np.float32),
             faces=np.zeros((0, 3), dtype=np.uint32))

    worker = gui_mesh_previewer._PreviewConvertWorker(
        1, str(src), str(out), "C:/fake/blender.exe", 120
    )
    worker.run()

    assert captured, "run_blender was never called"
    manifest = captured[0]
    assert manifest["mode"] == "preview_arrays"
    assert manifest["psk_path"] == str(src)
    assert manifest["output_path"] == str(out)


def test_convert_worker_emits_failed_when_blender_fails(
    qtbot, monkeypatch, tmp_path: Path
):
    """run_blender failure → worker.failed with the runner's message and
    no leftover .npz on disk."""
    monkeypatch.setattr(
        gui_mesh_previewer, "run_blender",
        lambda **kw: _FakeBlenderResult(success=False),
    )

    src = tmp_path / "model.fbx"
    src.write_bytes(b"Kaydara")
    out = tmp_path / "bogus.npz"
    out.write_bytes(b"leftover")

    worker = gui_mesh_previewer._PreviewConvertWorker(
        1, str(src), str(out), "C:/fake/blender.exe", 120, parent=None
    )
    with qtbot.waitSignal(worker.failed, timeout=2000) as blocker:
        worker.run()
    token, name, error = blocker.args
    assert token == 1
    assert name == "model.fbx"
    assert "stubbed failure" in error
    assert not out.exists()
