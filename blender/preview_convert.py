"""Blender headless script: convert a GLB/GLTF/FBX import into preview arrays.

Launched via:
    blender --background --python preview_convert.py -- manifest.json [--nonce <hex>]

Manifest JSON format:
{
    "psk_path": "path/model.glb",          # .glb/.gltf/.fbx input
    "output_path": "path/model.npz"        # geometry arrays target
}

Why this exists: the in-app mesh previewer parses PSK/PSKX directly via
``core.psk_reader``, but glTF/FBX have no Python parser there. Rather than
shipping another binary parser, headless Blender imports the file with its
bundled glTF/FBX importers and dumps plain numpy geometry arrays (.npz) the
viewer can build a ``PskMesh`` from.

Emits the same ``ASSET_STATUS`` wire protocol as process_asset.py:
``started`` / ``progress {step}`` / ``error`` / ``completed``.
"""

import argparse
import json
import os
import re
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = []

_parser = argparse.ArgumentParser(prog="preview_convert.py", add_help=False)
_parser.add_argument("manifest", nargs="?", help="Path to JSON manifest")
_parser.add_argument("--nonce", default="", help="Status-prefix nonce")
_args, _unknown = _parser.parse_known_args(argv)

_NONCE_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
NONCE = _args.nonce if _args.nonce and _NONCE_RE.match(_args.nonce) else ""
# ASSET_STATUS on purpose: run_blender parses that prefix regardless of
# which script produced the lines.
STATUS_PREFIX = f"##ASSET_STATUS:{NONCE}##" if NONCE else "##ASSET_STATUS##"


def status_msg(status: str, **kwargs):
    print(STATUS_PREFIX + json.dumps({"status": status, **kwargs}), flush=True)


if not _args.manifest:
    status_msg("error", message="No manifest path provided")
    sys.exit(1)

with open(_args.manifest, "r", encoding="utf-8") as f:
    manifest = json.load(f)


def _bail(msg: str):
    status_msg("error", message=msg)
    sys.exit(1)


# Manifest validation (defence in depth — the runner pre-checks the same).
_src = manifest.get("psk_path", "")
_out = manifest.get("output_path", "")
if not isinstance(_src, str) or not _src:
    _bail("Manifest missing psk_path")
if not isinstance(_out, str) or not _out:
    _bail("Manifest missing output_path")
_src_ext = os.path.splitext(_src)[1].lower()
if _src_ext not in (".glb", ".gltf", ".fbx"):
    _bail(f"Preview input must end in .glb/.gltf/.fbx (got {_src_ext!r})")
if not os.path.isfile(_src):
    _bail(f"Manifest psk_path does not exist: {_src}")
if not os.path.isabs(_out):
    _bail(f"Manifest output_path must be absolute: {_out}")
if os.path.splitext(_out)[1].lower() != ".npz":
    _bail(f"Manifest output_path must end in .npz: {_out}")


def _run_conversion():
    import numpy as np  # bundled with Blender's Python

    src = manifest["psk_path"]
    out_path = manifest["output_path"]
    src_ext = os.path.splitext(src)[1].lower()

    status_msg("started", asset=os.path.basename(src))

    # 0. Clean the factory scene first — `blender --background` still boots
    #   the default startup scene (Cube + Light + Camera), which would ride
    #   along into the geometry arrays otherwise. Mirrors the scene reset in
    #   process_asset.py. Objects first, then their datablocks.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)
    for arm in list(bpy.data.armatures):
        bpy.data.armatures.remove(arm)
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)

    # 1. Import with Blender's bundled importers (headless-safe ≥ 2.80).
    try:
        if src_ext in (".glb", ".gltf"):
            # filepath only — per Blender API docs, passing files=None to
            # this op triggers an RNA collection error.
            result = bpy.ops.import_scene.gltf(filepath=src)
            op_name = "import_scene.gltf"
        else:
            result = bpy.ops.import_scene.fbx(filepath=src)
            op_name = "import_scene.fbx"
    except Exception as e:
        _bail(f"Mesh import failed: {e}")

    if "CANCELLED" in (result or set()):
        _bail(f"{op_name} import cancelled by Blender for {src}")
    status_msg("progress", step="imported")

    mesh_objects = [o for o in bpy.data.objects if o.type == "MESH"]
    if not mesh_objects:
        _bail(f"Import produced no mesh objects for {src}")

    # 2. Extract triangulated corner attributes (position/normal/uv) in
    #    world space. to_mesh/from evaluated objects picks up all modifiers;
    #    every mesh obtained this way MUST be released via to_mesh_clear.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    corner_rows: list = []
    corner_total = 0

    for obj in mesh_objects:
        ob_eval = obj.evaluated_get(depsgraph)
        mesh = ob_eval.to_mesh()
        if mesh is None:
            continue
        try:
            if not mesh.vertices:
                continue
            # Explicit split on older Blender; a no-op on newer versions
            # where smooth loop normals are always available.
            try:
                mesh.calc_normals_split()
            except Exception:
                pass
            mesh.calc_loop_triangles()

            pos_local = np.array(
                [v.co[:] for v in mesh.vertices], dtype=np.float32
            )
            mworld = np.array(obj.matrix_world, dtype=np.float32)
            pos_world = pos_local @ mworld[:3, :3].T + mworld[:3, 3]

            uv_data = mesh.uv_layers[0].data if mesh.uv_layers else None
            loops = mesh.loops
            tri_loops = mesh.loop_triangles

            rows = np.empty((len(tri_loops) * 3, 8), dtype=np.float32)
            w = 0
            for tri in tri_loops:
                for li in tri.loops:
                    lp = loops[li]
                    vi = lp.vertex_index
                    rows[w, 0:3] = pos_world[vi]
                    n = lp.normal
                    rows[w, 3:6] = (n[0], n[1], n[2])
                    if uv_data is not None:
                        uv = uv_data[li].uv
                        rows[w, 6:8] = (uv[0], uv[1])
                    w += 1
            corner_rows.append(rows)
            corner_total += rows.shape[0]
        finally:
            ob_eval.to_mesh_clear()

    if not corner_rows or corner_total == 0:
        _bail(f"Import loaded no triangulated geometry for {src}")
    if corner_total % 3 != 0:
        _bail(
            f"Corner count {corner_total} is not a multiple of 3 "
            f"— corrupt import of {src}"
        )

    status_msg("progress", step="arrays_built")

    # 3. Dedupe corners into shared vertices — bounds the preview VBO to
    #    the unique-vertex count rather than a triangle-soup blowup.
    corners = np.concatenate(corner_rows, axis=0) if len(corner_rows) > 1 \
        else corner_rows[0]
    unique_rows, inverse = np.unique(corners, axis=0, return_inverse=True)
    verts = np.ascontiguousarray(unique_rows[:, 0:3], dtype=np.float32)
    normals = np.ascontiguousarray(unique_rows[:, 3:6], dtype=np.float32)
    uv0 = np.ascontiguousarray(unique_rows[:, 6:8], dtype=np.float32)
    faces = inverse.astype(np.uint32).reshape(-1, 3)

    if faces.shape[0] == 0:
        _bail(f"Import produced no triangles for {src}")

    # 4. Write the arrays. The consumer validates the file on load and
    #    deletes it on failure, so a partially-written npz is harmless.
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    np.savez(out_path, verts=verts, normals=normals, uv0=uv0, faces=faces)
    status_msg("progress", step="saved")

    status_msg("completed", asset=os.path.basename(src), verts=len(verts), faces=len(faces))


# ---------------------------------------------------------------------------
import bpy  # noqa: E402  (only available inside Blender)

_run_conversion()
