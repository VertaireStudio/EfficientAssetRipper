"""Embedded 3D mesh preview tab.

PSK/PSKX files parse directly via ``core.psk_reader`` and render in a
``QOpenGLWidget`` with an orbit camera. glTF (.glb/.gltf) and FBX files are
converted through a headless ``blender/preview_convert.py`` run into plain
numpy geometry arrays the same viewer can upload. Three render modes are
exposed in the toolbar: flat-lit color, UV checker grid, and wireframe.
"""

from __future__ import annotations

import atexit
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QObject, QPoint, QRunnable, QThreadPool, QThread, Signal, Slot
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QSurfaceFormat, QWheelEvent
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedLayout,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import gui.theme as theme
from core.blender_runner import run_blender, PREVIEW_SCRIPT
from core.psk_reader import PskMesh, PskParseError, _compute_bounds, read_psk

log = logging.getLogger(__name__)


def _wipe_temp_dir(path: Path) -> None:
    """atexit hook — remove the temp dir, swallowing all errors so a failure
    here can't poison interpreter shutdown."""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background loader
# ---------------------------------------------------------------------------

class _LoadSignals(QObject):
    loaded = Signal(int, object, str)   # token, PskMesh, name
    failed = Signal(int, str, str)      # token, name, error


class _LoadRunnable(QRunnable):
    def __init__(self, token: int, path: str, signals: _LoadSignals):
        super().__init__()
        self._token = token
        self._path = path
        self._signals = signals

    def run(self):
        name = Path(self._path).name
        try:
            mesh = read_psk(self._path)
        except (OSError, PskParseError, ValueError) as e:
            self._signals.failed.emit(self._token, name, str(e))
            return
        self._signals.loaded.emit(self._token, mesh, name)


# ---------------------------------------------------------------------------
# GLB / FBX preview conversion (headless Blender → .npz geometry arrays)
# ---------------------------------------------------------------------------

class _PreviewConvertWorker(QThread):
    """Runs headless Blender to convert a GLB/GLTF/FBX file into an .npz
    geometry dump the in-app viewer can render without another parser.

    The subprocess is driven by ``core.blender_runner.run_blender`` — the
    same runner the process/FBX pipelines use — so cancel support, timeouts
    and status parsing behave identically.
    """

    loaded = Signal(int, str)       # token, npz path
    failed = Signal(int, str, str)  # token, name, error

    def __init__(self, token: int, src_path: str, out_path: str,
                 blender_exe: str, timeout: int, parent=None):
        super().__init__(parent)
        self._token = token
        self._src = src_path
        self._out = out_path
        self._blender_exe = blender_exe
        self._timeout = timeout
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _safe_unlink(self):
        try:
            os.unlink(self._out)
        except OSError:
            pass

    def run(self):
        name = Path(self._src).name
        # mode: "preview_arrays" routes run_blender's manifest validation to
        # the .npz geometry-preview allowlist instead of the standard
        # .blend/.fbx process-asset rules.
        manifest = {
            "mode": "preview_arrays",
            "psk_path": self._src,
            "output_path": self._out,
        }
        try:
            result = run_blender(
                blender_exe=self._blender_exe,
                manifest=manifest,
                timeout=self._timeout,
                cancel_check=lambda: self._cancelled,
                script=PREVIEW_SCRIPT,
            )
        except Exception as e:  # noqa: BLE001 — the worker must never die silently
            log.exception("Preview conversion crashed for %s", self._src)
            self._safe_unlink()
            self.failed.emit(self._token, name, f"Unexpected error: {e}")
            return
        try:
            if not result.success:
                self._safe_unlink()
                self.failed.emit(
                    self._token, name,
                    result.error_message or "unknown error",
                )
                return
            if not os.path.isfile(self._out) or os.path.getsize(self._out) <= 0:
                self.failed.emit(
                    self._token, name, "Converter produced no geometry file"
                )
                return
            self.loaded.emit(self._token, self._out)
        except Exception as e:  # noqa: BLE001
            self._safe_unlink()
            self.failed.emit(self._token, name, str(e))


def _psk_mesh_from_npz(path: str | Path) -> PskMesh:
    """Load a preview-format .npz (verts/normals/uv0/faces) into a PskMesh.

    Raises on absent keys, shape mismatches, or truncated files so the
    caller can surface a clear failure and delete the file.

    The arrays (written by ``blender/preview_convert.py``) are unique-vertex
    indexed, which is exactly the wedge layout ``PskMesh`` expects.
    """
    path = Path(path)
    with np.load(path, allow_pickle=False) as data:
        verts = np.ascontiguousarray(data["verts"], dtype=np.float32)
        normals = np.ascontiguousarray(data["normals"], dtype=np.float32)
        uv0 = np.ascontiguousarray(data["uv0"], dtype=np.float32)
        faces = np.ascontiguousarray(data["faces"], dtype=np.uint32)

    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError(f"verts has bad shape {verts.shape}")
    if normals.shape != verts.shape:
        raise ValueError(f"normals has bad shape {normals.shape}")
    if uv0.shape != (verts.shape[0], 2):
        raise ValueError(f"uv0 has bad shape {uv0.shape}")
    if faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError(f"faces has bad shape {faces.shape}")
    if faces.size and int(faces.max()) >= verts.shape[0]:
        raise ValueError("faces reference vertices beyond the vertex array")

    bmin, bmax, center, radius = _compute_bounds(verts)
    return PskMesh(
        verts=verts,
        normals=normals,
        uv0=uv0,
        faces=faces,
        bounds_min=bmin,
        bounds_max=bmax,
        center=center,
        radius=radius,
    )


# ---------------------------------------------------------------------------
# GL shaders (GL 3.3 core)
# ---------------------------------------------------------------------------

_VERT_SHADER = """\
#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_normal;
layout(location = 2) in vec2 a_uv;
uniform mat4 u_mvp;
uniform mat4 u_model;
out vec3 v_normal_world;
out vec2 v_uv;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_normal_world = mat3(u_model) * a_normal;
    v_uv = a_uv;
}
"""

_FRAG_FLAT = """\
#version 330 core
in vec3 v_normal_world;
in vec2 v_uv;
uniform vec3 u_base_color;
uniform vec3 u_light_dir;
out vec4 frag_color;
void main() {
    vec3 N = normalize(v_normal_world);
    float lambert = max(dot(N, normalize(u_light_dir)), 0.0);
    vec3 col = u_base_color * (0.25 + 0.75 * lambert);
    frag_color = vec4(col, 1.0);
}
"""

_FRAG_UV = """\
#version 330 core
in vec3 v_normal_world;
in vec2 v_uv;
uniform sampler2D u_checker;
uniform vec3 u_light_dir;
out vec4 frag_color;
void main() {
    vec3 N = normalize(v_normal_world);
    float lambert = max(dot(N, normalize(u_light_dir)), 0.0);
    vec3 tex = texture(u_checker, v_uv).rgb;
    frag_color = vec4(tex * (0.4 + 0.6 * lambert), 1.0);
}
"""

_FRAG_WIRE = """\
#version 330 core
uniform vec3 u_wire_color;
out vec4 frag_color;
void main() {
    frag_color = vec4(u_wire_color, 1.0);
}
"""


# ---------------------------------------------------------------------------
# Math helpers (column-major mat4 layout — uniform upload format)
# ---------------------------------------------------------------------------

def _mat4_identity() -> np.ndarray:
    return np.eye(4, dtype=np.float32)


def _perspective(fov_y_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fov_y_deg) * 0.5)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / max(aspect, 1e-6)
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def _look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = target - eye
    f = f / max(np.linalg.norm(f), 1e-9)
    s = np.cross(f, up)
    s = s / max(np.linalg.norm(s), 1e-9)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    m[0, 3] = -np.dot(s, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] =  np.dot(f, eye)
    return m


# ---------------------------------------------------------------------------
# Quaternion helpers — orbit camera. Storing as numpy (w, x, y, z), float64
# for accumulation precision; only converted to mat3 for upload.
# Used instead of Euler azimuth/elevation so the camera can pass through the
# poles without gimbal lock.
# ---------------------------------------------------------------------------

_QUAT_IDENTITY = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)


def _quat_axis_angle(axis, angle: float) -> np.ndarray:
    a = np.asarray(axis, dtype=np.float64)
    n = np.linalg.norm(a)
    if n < 1e-12:
        return _QUAT_IDENTITY.copy()
    a = a / n
    half = angle * 0.5
    s = math.sin(half)
    return np.array([math.cos(half), a[0] * s, a[1] * s, a[2] * s], dtype=np.float64)


def _quat_mul(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    pw, px, py, pz = p
    qw, qx, qy, qz = q
    return np.array([
        pw * qw - px * qx - py * qy - pz * qz,
        pw * qx + px * qw + py * qz - pz * qy,
        pw * qy - px * qz + py * qw + pz * qx,
        pw * qz + px * qy - py * qx + pz * qw,
    ], dtype=np.float64)


def _quat_normalize(q: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(q)
    if n < 1e-12:
        return _QUAT_IDENTITY.copy()
    return q / n


def _quat_to_mat3(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array([
        [1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)],
        [2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)],
        [2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)],
    ], dtype=np.float32)


# ---------------------------------------------------------------------------
# OpenGL viewport widget
# ---------------------------------------------------------------------------

class _MeshGLView(QOpenGLWidget):
    """Renders a single mesh with an orbit camera."""

    mode_changed = Signal(str)
    mesh_loaded = Signal(int, int)   # vert_count, tri_count

    _MODES = ("flat", "uv", "wire")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mesh: Optional[PskMesh] = None
        self._mode: str = "flat"

        # Camera state — locked target, no panning. Quaternion orientation
        # gives full 360° orbit through the poles without gimbal lock.
        self._orientation: np.ndarray = self._default_orientation()
        self._distance = 3.0
        self._target = np.zeros(3, dtype=np.float32)

        self._last_mouse: Optional[QPoint] = None

        # GL resources, populated in initializeGL.
        self._vao: Optional[QOpenGLVertexArrayObject] = None
        self._vbo: Optional[QOpenGLBuffer] = None
        self._ebo: Optional[QOpenGLBuffer] = None
        self._index_count = 0
        self._programs: dict[str, QOpenGLShaderProgram] = {}
        self._checker: Optional[QOpenGLTexture] = None

        # Pending mesh waits for an active GL context before upload.
        self._pending_mesh: Optional[PskMesh] = None

        # Latches on first GL error so a broken driver / shader doesn't spam
        # the crash reporter on every repaint.
        self._gl_failed: bool = False

        # Theme-pulled colors used by paintGL — refreshed once at init.
        self._refresh_theme_colors()

        # 4× MSAA gives clean wireframe + lambert silhouettes.
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setSamples(4)
        self.setFormat(fmt)
        self.setMouseTracking(False)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _refresh_theme_colors(self):
        c = theme.current_scheme()
        self._bg_color = self._hex_to_rgb(c.get("bg_dark", "#181818"))
        self._wire_color = self._hex_to_rgb(c.get("accent", "#7AA8FF"))
        # Matte gray for flat-lit mode — readable against most backgrounds.
        self._flat_color = (0.62, 0.64, 0.67)

    @staticmethod
    def _default_orientation() -> np.ndarray:
        """Pleasant 3/4-front view: yaw ~35° then pitch ~20° down."""
        yaw = _quat_axis_angle((0, 1, 0), math.radians(35.0))
        pitch = _quat_axis_angle((1, 0, 0), math.radians(-20.0))
        return _quat_normalize(_quat_mul(yaw, pitch))

    @staticmethod
    def _hex_to_rgb(s: str) -> tuple[float, float, float]:
        s = s.lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            return (r, g, b)
        except (ValueError, IndexError):
            return (0.1, 0.1, 0.1)

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------

    def set_mesh(self, mesh: PskMesh):
        """Replace the rendered mesh. Safe to call before initializeGL."""
        self._mesh = mesh
        # New mesh = fresh chance for GL to succeed.
        self._gl_failed = False
        if mesh is not None:
            self._target = mesh.center.copy()
            self._distance = max(mesh.radius * 2.5, 0.5)
            # Reset to friendly default orientation each load.
            self._orientation = self._default_orientation()

        # Stat-strip listeners (and headless tests) only care that data arrived,
        # not whether GL has finished uploading. Emit unconditionally for non-None
        # meshes; the actual VBO upload defers if the context isn't ready yet.
        if mesh is not None:
            self.mesh_loaded.emit(mesh.vert_count, mesh.tri_count)

        if self.context() is None or not self.isValid():
            self._pending_mesh = mesh
            return

        self.makeCurrent()
        try:
            self._upload_mesh(mesh)
        finally:
            self.doneCurrent()
        self.update()

    def clear(self):
        """Drop the current mesh."""
        self.set_mesh(PskMesh(
            verts=np.zeros((0, 3), dtype=np.float32),
            normals=np.zeros((0, 3), dtype=np.float32),
            uv0=np.zeros((0, 2), dtype=np.float32),
            faces=np.zeros((0, 3), dtype=np.uint32),
        ))

    def set_render_mode(self, mode: str):
        if mode not in self._MODES:
            return
        if mode == self._mode:
            return
        self._mode = mode
        # Give the new shader a clean shot — the previous mode may have
        # latched the failure flag.
        self._gl_failed = False
        self.mode_changed.emit(mode)
        self.update()

    @property
    def render_mode(self) -> str:
        return self._mode

    def reset_view(self):
        if self._mesh is not None:
            self._distance = max(self._mesh.radius * 2.5, 0.5)
            self._target = self._mesh.center.copy()
        self._orientation = self._default_orientation()
        self.update()

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self):
        from OpenGL import GL  # local import — keeps PyOpenGL out of GUI thread until needed

        GL.glClearColor(*self._bg_color, 1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glEnable(GL.GL_CULL_FACE)
        GL.glCullFace(GL.GL_BACK)

        self._programs["flat"] = self._build_program(_VERT_SHADER, _FRAG_FLAT)
        self._programs["uv"]   = self._build_program(_VERT_SHADER, _FRAG_UV)
        self._programs["wire"] = self._build_program(_VERT_SHADER, _FRAG_WIRE)

        self._checker = self._build_checker_texture()

        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo.create()
        self._ebo = QOpenGLBuffer(QOpenGLBuffer.Type.IndexBuffer)
        self._ebo.create()

        if self._pending_mesh is not None:
            self._upload_mesh(self._pending_mesh)
            self._pending_mesh = None

    def resizeGL(self, w: int, h: int):
        from OpenGL import GL
        GL.glViewport(0, 0, w, h)

    def paintGL(self):
        from OpenGL import GL

        # Once GL has failed, just clear and return — never let the per-frame
        # exception reach the global crash handler again.
        if self._gl_failed:
            try:
                GL.glClearColor(*self._bg_color, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            except Exception:
                pass
            return

        try:
            self._render_frame(GL)
        except Exception as e:
            # Latch on first failure so subsequent repaints don't keep firing.
            self._gl_failed = True
            log.error("Mesh preview disabled — GL error: %s", e)
            try:
                GL.glClearColor(*self._bg_color, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            except Exception:
                pass

    def _render_frame(self, GL):
        # Hard state reset — never trust the previous frame. Without this,
        # leftover GL_LINE polygon mode from a wireframe pass leaks into the
        # next flat/UV pass, and CULL_FACE state accumulates across modes.
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        # Disable culling: PSK winding from CUE4Parse exports is mixed
        # (Unreal LH ↔ OpenGL RH conversion + some game-specific flips), so
        # culling either side leaves part of the mesh invisible. Doubling
        # back-face draws is cheap for preview.
        GL.glDisable(GL.GL_CULL_FACE)

        GL.glClearColor(*self._bg_color, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        if self._index_count == 0:
            return

        # Quaternion-based camera: orientation is applied as the rotation that
        # carries the world frame into camera frame. Eye starts on +Z relative
        # to the camera's local frame, then rotates with orientation so any
        # quaternion (no clamps, no poles) produces a valid look-at.
        rot = _quat_to_mat3(self._orientation)
        eye = self._target + (rot @ np.array([0, 0, 1.0], dtype=np.float32)) * self._distance
        up = rot @ np.array([0, 1.0, 0], dtype=np.float32)
        view = _look_at(eye, self._target, up)
        aspect = max(self.width(), 1) / max(self.height(), 1)
        # Near/far clamped to mesh scale to keep depth precision usable.
        radius = self._mesh.radius if self._mesh is not None else 1.0
        near = max(self._distance * 0.01, radius * 0.001)
        far  = max(self._distance + radius * 4.0, near * 1000.0)
        proj = _perspective(45.0, aspect, near, far)

        model = _mat4_identity()
        mvp = proj @ view @ model

        program = self._programs[self._mode]
        program.bind()

        # Use direct glUniform* calls everywhere. PySide6's setUniformValue
        # overload resolution from raw Python floats / ints is unreliable for
        # vec3 and sampler uniforms — that's what produced the original
        # UV-grid GL_INVALID_OPERATION crash and what was leaving the wire
        # shader's u_wire_color undefined (rendering near-black, "invisible").
        _set_uniform_mat4(GL, program, "u_mvp", mvp)
        _set_uniform_mat4(GL, program, "u_model", model)

        if self._mode == "flat":
            _set_uniform_3f(GL, program, "u_base_color", *self._flat_color)
            _set_uniform_3f(GL, program, "u_light_dir", 0.4, 0.7, 0.5)
        elif self._mode == "uv":
            _set_uniform_3f(GL, program, "u_light_dir", 0.4, 0.7, 0.5)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            self._checker.bind()
            _set_uniform_1i(GL, program, "u_checker", 0)
        else:  # wire
            _set_uniform_3f(GL, program, "u_wire_color", *self._wire_color)

        self._vao.bind()
        if self._mode == "wire":
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
            # Don't call glLineWidth — values other than 1.0 raise
            # GL_INVALID_VALUE on GL 3.3 core profile drivers that don't
            # support wide lines, which trips the gl_failed latch and leaves
            # the wireframe blank. 1.0 is the default and is universally
            # supported, so just rely on it.
            GL.glDrawElements(GL.GL_TRIANGLES, self._index_count, GL.GL_UNSIGNED_INT, None)
            # Polygon mode is reset at the top of the next frame — no need
            # to reset here. Resetting mid-frame after a draw was the path
            # that went wrong before (one stray exception left LINE sticky).
        else:
            GL.glDrawElements(GL.GL_TRIANGLES, self._index_count, GL.GL_UNSIGNED_INT, None)
        self._vao.release()
        program.release()

    # ------------------------------------------------------------------
    # GL helpers
    # ------------------------------------------------------------------

    def _build_program(self, vert_src: str, frag_src: str) -> QOpenGLShaderProgram:
        prog = QOpenGLShaderProgram(self)
        if not prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vert_src):
            log.error("Vertex shader compile failed: %s", prog.log())
        if not prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, frag_src):
            log.error("Fragment shader compile failed: %s", prog.log())
        prog.bindAttributeLocation("a_pos", 0)
        prog.bindAttributeLocation("a_normal", 1)
        prog.bindAttributeLocation("a_uv", 2)
        if not prog.link():
            log.error("Shader link failed: %s", prog.log())
        return prog

    def _build_checker_texture(self) -> QOpenGLTexture:
        """Generate a 256×256 RGBA checker pattern in code (no asset file)."""
        size = 256
        cells = 8
        cell_px = size // cells
        # Two-tone checker with mild contrast. Dark/light cells alternate.
        a = np.array([60, 70, 80, 255], dtype=np.uint8)
        b = np.array([200, 205, 210, 255], dtype=np.uint8)
        img = np.zeros((size, size, 4), dtype=np.uint8)
        for cy in range(cells):
            for cx in range(cells):
                col = a if (cx + cy) % 2 == 0 else b
                y0, y1 = cy * cell_px, (cy + 1) * cell_px
                x0, x1 = cx * cell_px, (cx + 1) * cell_px
                img[y0:y1, x0:x1] = col

        # Tint top-left cell with a clear color marker so UV orientation reads at a glance.
        img[0:cell_px, 0:cell_px] = np.array([220, 80, 80, 255], dtype=np.uint8)

        # Wrap as a copied QImage (numpy buffer would be reclaimed otherwise).
        qimg = QImage(img.tobytes(), size, size, size * 4, QImage.Format.Format_RGBA8888).copy()
        tex = QOpenGLTexture(qimg, QOpenGLTexture.MipMapGeneration.GenerateMipMaps)
        tex.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
        tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        tex.setWrapMode(QOpenGLTexture.WrapMode.Repeat)
        return tex

    def _upload_mesh(self, mesh: Optional[PskMesh]):
        from OpenGL import GL

        if mesh is None or mesh.faces.size == 0 or mesh.verts.size == 0:
            self._index_count = 0
            return

        # Interleave: pos(3) + normal(3) + uv(2) = 8 floats per vertex.
        n = mesh.verts.shape[0]
        interleaved = np.empty((n, 8), dtype=np.float32)
        interleaved[:, 0:3] = mesh.verts
        interleaved[:, 3:6] = mesh.normals
        interleaved[:, 6:8] = mesh.uv0
        vbo_data = interleaved.tobytes()
        ebo_data = mesh.faces.astype(np.uint32, copy=False).tobytes()

        self._vao.bind()

        self._vbo.bind()
        self._vbo.allocate(vbo_data, len(vbo_data))

        self._ebo.bind()
        self._ebo.allocate(ebo_data, len(ebo_data))

        stride = 8 * 4  # bytes
        # Attrib 0 — position
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, None)
        # Attrib 1 — normal
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, _gl_offset(3 * 4))
        # Attrib 2 — uv
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 2, GL.GL_FLOAT, GL.GL_FALSE, stride, _gl_offset(6 * 4))

        self._vao.release()
        self._vbo.release()
        # Index buffer must remain bound to the VAO; release after VAO.release().
        self._ebo.release()

        self._index_count = int(mesh.faces.size)

    # ------------------------------------------------------------------
    # Mouse / wheel — orbit + zoom only (no panning)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_mouse = event.position().toPoint()

    def mouseMoveEvent(self, event: QMouseEvent):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._last_mouse is None:
            self._last_mouse = event.position().toPoint()
            return
        cur = event.position().toPoint()
        dx = cur.x() - self._last_mouse.x()
        dy = cur.y() - self._last_mouse.y()
        self._last_mouse = cur

        # Trackball orbit: both axes are screen-relative (rotations applied in
        # the camera's local frame). Drag-X spins around the screen vertical
        # (= camera local Y), drag-Y spins around the screen horizontal (=
        # camera local X). This is invariant to the model's up-axis convention
        # — Z-up Unreal meshes, Y-up FBX, weird per-game flips all orbit the
        # same way. World-Y "turntable" orbit fails on Z-up models because
        # world Y is sideways relative to the model's true vertical, which is
        # exactly what was reported as "rotates over some weird sideways axis".
        scale = 0.005
        yaw_q = _quat_axis_angle((0.0, 1.0, 0.0), -dx * scale)
        pitch_q = _quat_axis_angle((1.0, 0.0, 0.0), -dy * scale)
        self._orientation = _quat_normalize(
            _quat_mul(self._orientation, _quat_mul(yaw_q, pitch_q))
        )
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_mouse = None

    def wheelEvent(self, event: QWheelEvent):
        # 120 units per notch on a normal mouse wheel.
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 0.9 if delta > 0 else 1.1
        radius = self._mesh.radius if self._mesh is not None else 1.0
        new_dist = self._distance * factor
        # Clamp to a sensible range relative to mesh size.
        self._distance = float(np.clip(new_dist, radius * 0.2, radius * 50.0))
        self.update()


def _gl_offset(byte_offset: int):
    """Return a ctypes-compatible offset for glVertexAttribPointer.

    PyOpenGL accepts None for offset 0 and a ctypes void pointer for non-zero
    offsets. Using `ctypes.c_void_p(byte_offset)` avoids the "expected pointer"
    deprecation warning some PyOpenGL builds emit on raw int args.
    """
    import ctypes
    return ctypes.c_void_p(byte_offset) if byte_offset else None


def _set_uniform_mat4(GL, program, name: str, mat: np.ndarray) -> None:
    """Upload a 4×4 numpy matrix to a mat4 uniform. Row-major in numpy →
    transpose=GL_TRUE for OpenGL's column-major expectation.

    Direct glUniformMatrix4fv bypasses PySide6's setUniformValue overload
    ambiguity that was the root cause of the original UV-grid crash."""
    loc = program.uniformLocation(name)
    if loc < 0:
        return
    flat = np.ascontiguousarray(mat, dtype=np.float32).flatten()
    GL.glUniformMatrix4fv(loc, 1, GL.GL_TRUE, flat)


def _set_uniform_3f(GL, program, name: str, x: float, y: float, z: float) -> None:
    loc = program.uniformLocation(name)
    if loc < 0:
        return
    GL.glUniform3f(loc, float(x), float(y), float(z))


def _set_uniform_1i(GL, program, name: str, value: int) -> None:
    loc = program.uniformLocation(name)
    if loc < 0:
        return
    GL.glUniform1i(loc, int(value))


# ---------------------------------------------------------------------------
# Tab widget
# ---------------------------------------------------------------------------

class MeshPreviewerPanel(QWidget):
    """Right-tab panel for previewing PSK / PSKX meshes."""

    log_message = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_path: str = ""
        self._load_token = 0
        self._load_signals = _LoadSignals()
        self._load_signals.loaded.connect(self._on_load_done)
        self._load_signals.failed.connect(self._on_load_failed)

        # Active GLB/FBX→npz conversion. Reaped like the picker's search
        # worker: a running worker is cleaned up by its own signals, an idle
        # one by deleteLater at next start.
        self._convert_worker: _PreviewConvertWorker | None = None

        # Temp dir for temp-export-for-preview from the unpacker. Mirrors the
        # AudioPreviewerPanel pattern — gives the unpacker a place to drop
        # preview-only PSKs that never pollute the user's real export folder.
        self._temp_dir = Path(tempfile.mkdtemp(prefix="ear_mesh_preview_"))
        atexit.register(_wipe_temp_dir, self._temp_dir)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Toolbar ────────────────────────────────────────────────────
        toolbar = QToolBar()
        toolbar.setMovable(False)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        def _add_mode_button(label: str, mode: str, checked: bool = False) -> QPushButton:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(checked)
            btn.clicked.connect(lambda _=False, m=mode: self._gl.set_render_mode(m))
            self._mode_group.addButton(btn)
            toolbar.addWidget(btn)
            return btn

        self._btn_flat = _add_mode_button("Flat", "flat", checked=True)
        self._btn_uv   = _add_mode_button("UV Grid", "uv")
        self._btn_wire = _add_mode_button("Wireframe", "wire")

        toolbar.addSeparator()
        self._btn_reset = QPushButton("Reset View")
        self._btn_reset.clicked.connect(lambda: self._gl.reset_view())
        toolbar.addWidget(self._btn_reset)

        layout.addWidget(toolbar)

        # ── Stacked content: empty placeholder vs GL viewport ─────────
        self._stack_host = QWidget()
        self._stack = QStackedLayout(self._stack_host)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._placeholder = QLabel(
            "Right-click a mesh row in the Asset Browser → Preview Mesh\n"
            "(or right-click an exported .uasset in the Unpacker)\n\n"
            "PSK/PSKX parse directly; GLB/GLTF/FBX convert via Blender"
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._stack.addWidget(self._placeholder)

        self._gl = _MeshGLView(self)
        self._gl.mode_changed.connect(self._on_mode_changed)
        self._gl.mesh_loaded.connect(self._on_mesh_loaded)
        self._stack.addWidget(self._gl)

        self._stack.setCurrentWidget(self._placeholder)
        layout.addWidget(self._stack_host, stretch=1)

        # ── Status strip ───────────────────────────────────────────────
        status_row = QHBoxLayout()
        status_row.setContentsMargins(8, 4, 8, 4)
        self._status_name = QLabel("")
        status_row.addWidget(self._status_name, 1)
        self._status_stats = QLabel("")
        status_row.addWidget(self._status_stats, 0)
        status_host = QWidget()
        status_host.setLayout(status_row)
        layout.addWidget(status_host)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def temp_dir(self) -> Path:
        """Temp directory the unpacker can write preview-only PSKs into."""
        return self._temp_dir

    def load_mesh(self, path: str | Path):
        """Parse and display a mesh file. Heavy work runs off the GUI thread.

        PSK/PSKX parse directly in-process. GLB/GLTF/FBX go through a
        headless Blender conversion into geometry arrays first.
        """
        path_str = str(path)
        self._current_path = path_str
        name = Path(path_str).name

        if not Path(path_str).is_file():
            self._status_name.setText(f"Not found: {name}")
            self._status_stats.setText("")
            self._stack.setCurrentWidget(self._placeholder)
            self._placeholder.setText(f"File not found:\n{path_str}")
            return

        suffix = Path(path_str).suffix.lower()
        if suffix in (".glb", ".gltf", ".fbx"):
            self._start_conversion(path_str)
            return

        if suffix not in (".psk", ".pskx"):
            self._status_name.setText(f"Unsupported: {name}")
            self._status_stats.setText("")
            self._stack.setCurrentWidget(self._placeholder)
            self._placeholder.setText(
                f"Unsupported mesh format:\n{path_str}\n\n"
                "Supported: .psk, .pskx, .glb, .gltf, .fbx"
            )
            return

        self._load_token += 1
        token = self._load_token
        self._status_name.setText(f"Loading: {name}…")
        self._status_stats.setText("")

        runnable = _LoadRunnable(token, path_str, self._load_signals)
        QThreadPool.globalInstance().start(runnable)

    def load_psk(self, path: str | Path):
        """Backward-compatible alias for :meth:`load_mesh`."""
        self.load_mesh(path)

    # ------------------------------------------------------------------
    # GLB / FBX conversion plumbing
    # ------------------------------------------------------------------

    def _start_conversion(self, src_path: str):
        """Convert a GLB/GLTF/FBX file via headless Blender for preview."""
        import config as _config  # local import — keep Qt-free at module load

        name = Path(src_path).name
        blender_exe = _config.get("blender_exe")
        if not blender_exe:
            self._status_name.setText("Blender not configured")
            self._status_stats.setText("")
            self._stack.setCurrentWidget(self._placeholder)
            self._placeholder.setText(
                f"Previewing {name} requires Blender.\n\n"
                "Set the Blender executable path in Settings."
            )
            self.log_message.emit(
                "Mesh preview needs a Blender executable (Settings) to "
                f"convert {name}", "warning",
            )
            return

        self._load_token += 1
        token = self._load_token

        out_path = self._temp_dir / f"preview_{token}_{Path(name).stem}.npz"
        try:
            if out_path.exists():
                os.unlink(out_path)
        except OSError:
            pass

        self._status_name.setText(f"Converting: {name}…")
        self._status_stats.setText("")
        self._stack.setCurrentWidget(self._placeholder)
        self._placeholder.setText(
            f"Converting {name} via Blender…\n\n"
            "The first conversion starts slower (Blender startup)."
        )

        # Reap the previous worker so repeated previews don't accumulate
        # QThread instances parented to this panel (leak).
        old = self._convert_worker
        self._convert_worker = None
        if old is not None:
            try:
                running = old.isRunning()
            except RuntimeError:
                # Its queued deleteLater already ran — the C++ object is
                # gone and the Python wrapper is just a husk.
                running = False
            if running:
                old.loaded.connect(old.deleteLater)
                old.failed.connect(old.deleteLater)
            else:
                try:
                    old.deleteLater()
                except RuntimeError:
                    pass  # already destroyed

        try:
            timeout = int(_config.get_int("timeout_seconds") or 120)
        except Exception:  # noqa: BLE001 — config read is best-effort
            timeout = 120

        self._convert_worker = _PreviewConvertWorker(
            token, src_path, str(out_path), blender_exe, timeout, self
        )
        self._convert_worker.loaded.connect(self._on_convert_done)
        self._convert_worker.failed.connect(self._on_convert_failed)
        self._convert_worker.loaded.connect(self._convert_worker.deleteLater)
        self._convert_worker.failed.connect(self._convert_worker.deleteLater)
        self._convert_worker.start()

    def _on_convert_done(self, token: int, npz_path: str):
        """Build a PskMesh from the converter's .npz output."""
        self._convert_worker = None
        if token != self._load_token:
            return
        name = Path(self._current_path).name
        try:
            mesh = _psk_mesh_from_npz(npz_path)
        except Exception as e:  # noqa: BLE001 — surface any array corruption
            log.warning("Preview npz load failed for %s: %s", npz_path, e)
            self._delete_temp_file(npz_path)
            self._on_convert_failed(token, name, f"conversion output invalid: {e}")
            return
        # Delete the geometry dump once it's in memory — the temp dir holds
        # one file per preview attempt, and stale previews would accumulate.
        self._delete_temp_file(npz_path)
        self._gl.set_mesh(mesh)
        self._stack.setCurrentWidget(self._gl)
        self._status_name.setText(name)

    def _on_convert_failed(self, token: int, name: str, error: str):
        self._convert_worker = None
        if token != self._load_token:
            return
        self._status_name.setText(f"Failed: {name}")
        self._status_stats.setText("")
        self._stack.setCurrentWidget(self._placeholder)
        self._placeholder.setText(f"Failed to preview {name}\n{error}")
        self.log_message.emit(f"Mesh preview error: {error}", "error")

    @staticmethod
    def _delete_temp_file(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def clear(self):
        self._gl.clear()
        self._status_name.setText("")
        self._status_stats.setText("")
        self._stack.setCurrentWidget(self._placeholder)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_load_done(self, token: int, mesh: object, name: str):
        if token != self._load_token:
            return
        if not isinstance(mesh, PskMesh):
            self._on_load_failed(token, name, "loader returned non-mesh")
            return
        self._gl.set_mesh(mesh)
        self._stack.setCurrentWidget(self._gl)
        self._status_name.setText(name)

    def _on_load_failed(self, token: int, name: str, error: str):
        if token != self._load_token:
            return
        self._status_name.setText(f"Failed: {name}")
        self._status_stats.setText(error)
        self._stack.setCurrentWidget(self._placeholder)
        self._placeholder.setText(f"Failed to load {name}\n{error}")
        self.log_message.emit(f"Mesh preview error: {error}", "error")

    def _on_mode_changed(self, mode: str):
        self._btn_flat.setChecked(mode == "flat")
        self._btn_uv.setChecked(mode == "uv")
        self._btn_wire.setChecked(mode == "wire")

    def _on_mesh_loaded(self, vert_count: int, tri_count: int):
        self._status_stats.setText(f"{vert_count:,} verts · {tri_count:,} tris")
