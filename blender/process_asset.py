"""Blender headless processing script.

Launched via:
    blender --background --python process_asset.py -- manifest.json [--nonce <hex>]

Reads a JSON manifest, imports PSK/PSKX, wires materials, saves .blend.
Prints structured JSON status lines to stdout for the GUI to parse.

When ``--nonce <hex>`` is supplied, status lines carry the prefix
``##ASSET_STATUS:<hex>##``.  Without a nonce we fall back to the legacy
``##ASSET_STATUS##`` prefix so older harnesses still work.
"""

import argparse
import json
import os
import re
import sys
import traceback

# ---------------------------------------------------------------------------
# Parse args — everything after "--" belongs to us
# ---------------------------------------------------------------------------
argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = []

_parser = argparse.ArgumentParser(prog="process_asset.py", add_help=False)
_parser.add_argument("manifest", nargs="?", help="Path to JSON manifest")
_parser.add_argument("--nonce", default="", help="Status-prefix nonce")
_parser.add_argument(
    "--status-file",
    default="",
    help="Optional path to mirror status lines into (one JSON per line).",
)
_args, _unknown = _parser.parse_known_args(argv)

_NONCE_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
NONCE = _args.nonce if _args.nonce and _NONCE_RE.match(_args.nonce) else ""
STATUS_PREFIX = f"##ASSET_STATUS:{NONCE}##" if NONCE else "##ASSET_STATUS##"
STATUS_FILE = _args.status_file or ""


def status_msg(status: str, **kwargs):
    """Print a JSON status line to stdout (and optional status file)."""
    msg = {"status": status, **kwargs}
    line = STATUS_PREFIX + json.dumps(msg)
    print(line, flush=True)
    if STATUS_FILE:
        try:
            with open(STATUS_FILE, "a", encoding="utf-8") as _sf:
                _sf.write(json.dumps(msg) + "\n")
        except OSError:
            pass


if not _args.manifest:
    status_msg("error", message="No manifest path provided")
    sys.exit(1)

manifest_path = _args.manifest

with open(manifest_path, "r", encoding="utf-8") as f:
    manifest = json.load(f)


# ---------------------------------------------------------------------------
# Manifest validation (defence in depth — runner already pre-checks).
# ---------------------------------------------------------------------------

# Mesh input formats accepted in the standard mode. Mirrored from
# core/blender_runner.py (defence in depth — the runner also pre-checks).
_MESH_INPUT_EXTS = {".psk", ".pskx", ".glb", ".gltf", ".fbx"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr",
               ".hdr", ".bmp", ".webp", ".dds"}


def _bail(msg: str):
    status_msg("error", message=msg)
    sys.exit(1)


def _validate_manifest(m: dict):
    psk_path = m.get("psk_path", "")
    output_path = m.get("output_path", "")
    outputs_root = m.get("outputs_root", "")

    if not isinstance(psk_path, str) or not psk_path:
        _bail("Manifest missing psk_path")
    if not isinstance(output_path, str) or not output_path:
        _bail("Manifest missing output_path")

    psk_ext = os.path.splitext(psk_path)[1].lower()
    if psk_ext not in _MESH_INPUT_EXTS:
        _bail(
            "Manifest psk_path must end in .psk/.pskx/.glb/.gltf/.fbx "
            f"(got {psk_ext!r})"
        )
    if not os.path.isfile(psk_path):
        _bail(f"Manifest psk_path does not exist: {psk_path}")

    if not os.path.isabs(output_path):
        _bail(f"Manifest output_path must be absolute: {output_path}")
    # ".blend" = the standard processing output; ".fbx" = the FBX export
    # path (mesh format "fbx"). Anything else is a typo/adversarial path.
    out_ext = os.path.splitext(output_path)[1].lower()
    if out_ext not in (".blend", ".fbx"):
        _bail(
            f"Manifest output_path must end in .blend or .fbx: {output_path}"
        )

    if outputs_root:
        norm_root = os.path.normpath(outputs_root)
        norm_out = os.path.normpath(output_path)
        try:
            common = os.path.commonpath([norm_out, norm_root])
        except ValueError:
            common = ""
        if common != norm_root:
            _bail(
                f"Manifest output_path {output_path!r} escapes outputs_root "
                f"{outputs_root!r}"
            )

    materials_spec = m.get("materials", {}) or {}
    for mat_name, spec in materials_spec.items():
        for slot, tex_info in (spec.get("textures", {}) or {}).items():
            tex_path = tex_info.get("path", "") if isinstance(tex_info, dict) else ""
            if not tex_path:
                _bail(f"Material {mat_name!r} slot {slot!r}: missing texture path")
            tex_ext = os.path.splitext(tex_path)[1].lower()
            if tex_ext not in _IMAGE_EXTS:
                _bail(
                    f"Material {mat_name!r} slot {slot!r}: unsupported texture "
                    f"extension {tex_ext!r} ({tex_path})"
                )
            if not os.path.isfile(tex_path):
                _bail(
                    f"Material {mat_name!r} slot {slot!r}: texture not found "
                    f"({tex_path})"
                )


_validate_manifest(manifest)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

import bpy  # noqa: E402  (only available inside Blender)

# Add parent dir to path so we can import material_setup
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from material_setup import setup_material_textures  # noqa: E402


def main():
    psk_path = manifest["psk_path"]
    output_path = manifest["output_path"]
    output_ext = os.path.splitext(output_path)[1].lower()  # ".blend" | ".fbx"
    addon_name = manifest.get("addon_name", "bl_ext.blender_org.io_scene_psk_psa")
    materials_spec = manifest.get("materials", {})

    status_msg("started", asset=os.path.basename(psk_path))

    # 1. Enable PSK import addon (before any scene reset so extension system is intact)
    try:
        bpy.ops.preferences.addon_enable(module=addon_name)
        status_msg("progress", step="addon_enabled")
    except Exception as e:
        status_msg("warning", message=f"Could not enable addon {addon_name}: {e}")

    # 2. Clean scene — remove all objects, meshes, materials, etc.
    #    (Don't use read_factory_settings as it wipes the extension system)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)
    for arm in list(bpy.data.armatures):
        bpy.data.armatures.remove(arm)

    # 3. Import the mesh — dispatch by extension. PSK/PSKX go through the
    # PSK addon; GLB/GLTF and FBX use Blender's bundled importers (both
    # callable headless in Blender ≥ 2.80).
    try:
        ext = os.path.splitext(psk_path)[1].lower()
        if ext in (".psk", ".pskx"):
            result = bpy.ops.psk.import_file(filepath=psk_path)
            op_name = "psk.import_file"
        elif ext in (".glb", ".gltf"):
            # filepath only — per Blender API docs, passing files=None to
            # this op triggers an RNA collection error.
            result = bpy.ops.import_scene.gltf(filepath=psk_path)
            op_name = "import_scene.gltf"
        elif ext == ".fbx":
            result = bpy.ops.import_scene.fbx(filepath=psk_path)
            op_name = "import_scene.fbx"
        else:
            status_msg("error", message=f"Unsupported file type: {ext}")
            sys.exit(1)
        # bpy.ops returns a set like {"FINISHED"}, {"CANCELLED"}, etc.
        if "CANCELLED" in (result or set()):
            status_msg(
                "error",
                message=f"{op_name} import cancelled by Blender for {psk_path}",
            )
            sys.exit(1)
        if not any(o.type == "MESH" for o in bpy.data.objects):
            status_msg(
                "error",
                message=f"Mesh import produced no mesh objects for {psk_path}",
            )
            sys.exit(1)
        # Wire token kept as "psk_imported" (informational only; fixtures).
        status_msg("progress", step="psk_imported")
    except Exception as e:
        status_msg("error", message=f"Mesh import failed: {e}")
        sys.exit(1)

    # 4. Wire materials
    processed_materials = 0
    failed_materials = 0

    # Geometry-only FBX exports carry no material manifest — skip the wiring
    # loop entirely instead of warning once per imported material about specs
    # that were never meant to exist. (The empty iterable short-circuits the
    # loop below; everything else stays as the .blend path expects.)
    skip_material_wiring = output_ext == ".fbx" and not materials_spec
    if skip_material_wiring:
        status_msg("progress", step="materials_skipped")

    for obj in [] if skip_material_wiring else bpy.data.objects:
        if obj.type != "MESH":
            continue

        for mat_slot in obj.material_slots:
            mat = mat_slot.material
            if mat is None:
                continue

            mat_name = mat.name

            # Find matching material spec in manifest
            # Try exact match on slot name first, then material name
            mat_spec = None
            for slot_name, spec in materials_spec.items():
                if slot_name == mat_name or spec.get("material_name") == mat_name:
                    mat_spec = spec
                    break

            if mat_spec is None:
                # Surface the available manifest keys so name-mismatch
                # diagnoses don't require trawling the full status log.
                # Keep the listing bounded — large meshes can hit hundreds.
                available = list(materials_spec.keys())[:8]
                status_msg(
                    "warning",
                    message=(
                        f"No texture spec for material: {mat_name}"
                        f" (available: {available}{'…' if len(materials_spec) > 8 else ''})"
                    ),
                )
                continue

            textures = mat_spec.get("textures", {})
            bsdf_overrides = mat_spec.get("bsdf_overrides", {})
            color_tints = mat_spec.get("color_tints", {})

            if not textures:
                continue

            try:
                setup_material_textures(
                    mat,
                    textures,
                    bsdf_overrides,
                    color_tints,
                    on_warning=lambda m: status_msg("warning", message=m),
                )
                processed_materials += 1
                status_msg("progress", step="material_wired", material=mat_name)
            except Exception as e:
                failed_materials += 1
                status_msg("warning", message=f"Failed to wire {mat_name}: {e}")
                traceback.print_exc()

    # 5. Remove armature, keep mesh, clean up names
    # .blend output strips the armature deliberately (the .blend viewer flow
    # only wants the mesh); FBX output must keep it so the rig/skin weights
    # survive the export.
    if output_ext == ".blend":
        for obj in list(bpy.data.objects):
            if obj.type == "ARMATURE":
                # Capture each child's world matrix before clearing the parent so
                # the visual transform survives the unparent.  Reading
                # `child.matrix_world` after `parent = None` would give the new
                # local matrix, not the world one we wanted to preserve.
                for child in list(obj.children):
                    world = child.matrix_world.copy()
                    child.parent = None
                    child.matrix_world = world
                bpy.data.objects.remove(obj, do_unlink=True)
        for arm in list(bpy.data.armatures):
            bpy.data.armatures.remove(arm)

    # Strip trailing .001 / .002 etc. from object and mesh names
    for obj in bpy.data.objects:
        cleaned = re.sub(r"\.\d{3}$", "", obj.name)
        if cleaned != obj.name:
            obj.name = cleaned
        if obj.data and hasattr(obj.data, "name"):
            data_cleaned = re.sub(r"\.\d{3}$", "", obj.data.name)
            if data_cleaned != obj.data.name:
                obj.data.name = data_cleaned

    # 6. Set viewport clip start/end + focus on object
    # In headless (--background) mode bpy.context.screen.areas is empty,
    # so we set clip values via bpy.data.screens which persists into the
    # saved .blend regardless of whether we're headless or interactive.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.clip_start = 0.1
                        space.clip_end = 100000

    if not bpy.app.background:

        bpy.ops.object.select_all(action="DESELECT")
        mesh_obj = None
        for obj in bpy.data.objects:
            if obj.type == "MESH":
                obj.select_set(True)
                mesh_obj = obj
        if mesh_obj:
            bpy.context.view_layer.objects.active = mesh_obj
            for area in bpy.context.screen.areas:
                if area.type == "VIEW_3D":
                    with bpy.context.temp_override(area=area):
                        bpy.ops.view3d.view_selected()

    # 7. Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 8. Save .blend atomically / export .fbx atomically — write to .tmp,
    # replace on success.
    if output_ext == ".fbx":
        tmp_output = output_path + ".tmp"
        try:
            bpy.ops.export_scene.fbx(
                filepath=tmp_output,
                # Whole scene: armature + skinned mesh, nothing selected.
                use_selection=False,
                object_types={"ARMATURE", "MESH"},
                # PSK imports carry no action data; baking just slows the
                # export and bakes nothing useful.
                bake_anim=False,
                add_leaf_bones=False,
                # Geometry-only by design: texture files are not inlined;
                # material references still point at their image paths.
                embed_textures=False,
                path_mode="AUTO",
            )
            try:
                tmp_size = os.path.getsize(tmp_output)
            except OSError:
                tmp_size = 0
            if tmp_size <= 0:
                try:
                    os.unlink(tmp_output)
                except OSError:
                    pass
                status_msg("error", message="Exported FBX was empty (0 bytes) — aborting")
                sys.exit(1)
            os.replace(tmp_output, output_path)
            status_msg("progress", step="saved")
        except Exception as e:
            try:
                os.unlink(tmp_output)
            except OSError:
                pass
            status_msg("error", message=f"Failed to export FBX: {e}")
            sys.exit(1)
    else:
        tmp_output = output_path + ".tmp"
        try:
            bpy.ops.wm.save_as_mainfile(filepath=tmp_output)
            try:
                tmp_size = os.path.getsize(tmp_output)
            except OSError:
                tmp_size = 0
            if tmp_size <= 0:
                try:
                    os.unlink(tmp_output)
                except OSError:
                    pass
                status_msg("error", message="Saved .blend was empty (0 bytes) — aborting")
                sys.exit(1)
            os.replace(tmp_output, output_path)
            status_msg("progress", step="saved")
        except Exception as e:
            try:
                os.unlink(tmp_output)
            except OSError:
                pass
            status_msg("error", message=f"Failed to save: {e}")
            sys.exit(1)

    status_msg(
        "completed",
        asset=os.path.basename(psk_path),
        materials_processed=processed_materials,
        materials_failed=failed_materials,
    )


try:
    main()
except SystemExit:
    raise
except Exception as e:
    status_msg("error", message=str(e))
    traceback.print_exc()
    sys.exit(1)
