"""Full asset discovery pipeline.

Scans game folder for PSK/PSKX files via Everything SDK, resolves their
materials and textures, and produces a list of AssetEntry objects ready
for batch processing.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.everything import EverythingSDK, get_sdk
from core.classifier import AssetCategory, classify
from core.props_parser import (
    MaterialRef,
    parse_material_props_file,
    parse_mesh_props_file,
)
from core.texture_resolver import (
    ResolvedTexture,
    TextureResolution,
    UnresolvedTexture,
    resolve_textures,
)

log = logging.getLogger(__name__)


# Hard cap for parent-chain traversal. Real UE material chains rarely exceed
# 4-5 levels; 32 is a generous safety net before we declare a cycle.
MAX_PARENT_DEPTH = 32


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MaterialEntry:
    """Resolved material with its textures."""
    slot_name: str
    material_name: str
    textures: list[ResolvedTexture] = field(default_factory=list)
    unresolved: list[UnresolvedTexture] = field(default_factory=list)
    props_found: bool = True
    preset_used: str = "default_pbr"
    bsdf_overrides: dict = field(default_factory=dict)
    color_tints: dict = field(default_factory=dict)
    scalar_params: dict = field(default_factory=dict)
    parent_name: str = ""
    keyword_fallback_used: list[str] = field(default_factory=list)


@dataclass
class AssetEntry:
    """A fully-resolved PSK/PSKX asset ready for processing."""
    psk_path: Path
    name: str
    materials: list[MaterialEntry] = field(default_factory=list)
    mesh_props_found: bool = True
    total_textures: int = 0
    missing_textures: int = 0
    category: str = "Uncategorized"
    subcategory: str = "General"
    blend_path: Optional[Path] = None
    processed: bool = False

    @property
    def status(self) -> str:
        if self.processed:
            return "processed"
        if not self.mesh_props_found:
            return "no_props"
        if self.missing_textures > 0:
            return "missing_textures"
        if not self.materials:
            return "no_materials"
        return "ready"

    @property
    def status_text(self) -> str:
        s = self.status
        if s == "processed":
            return "Processed"
        if s == "ready":
            return f"Ready ({self.total_textures} textures)"
        elif s == "missing_textures":
            return f"Missing {self.missing_textures}/{self.total_textures}"
        elif s == "no_props":
            return "No .props.txt found"
        elif s == "no_materials":
            return "No materials"
        return s

    def to_manifest(self, output_path: Path, addon_name: str) -> dict:
        """Convert to a JSON manifest for the Blender processing script."""
        materials = {}
        for mat in self.materials:
            textures = {}
            for tex in mat.textures:
                textures[tex.slot] = {
                    "path": str(tex.path),
                    "colorspace": tex.colorspace,
                    "wiring": tex.wiring,
                }
            materials[mat.slot_name] = {
                "material_name": mat.material_name,
                "textures": textures,
                "bsdf_overrides": mat.bsdf_overrides,
                "color_tints": mat.color_tints,
            }

        return {
            "psk_path": str(self.psk_path),
            "output_path": str(output_path),
            "addon_name": addon_name,
            "materials": materials,
        }


# ---------------------------------------------------------------------------
# PSK binary material extraction
# ---------------------------------------------------------------------------

# Known PSK/PSKX chunk IDs. Anything outside this set is logged at debug
# level and skipped — older or game-specific exports may add custom chunks.
_KNOWN_PSK_CHUNK_IDS = frozenset({
    "ACTRHEAD",
    "PNTS0000",
    "VTXW0000",
    "FACE0000",
    "FACE3200",
    "MATT0000",
    "REFSKELT", "REFSKEL0",
    "RAWWEIGHTS", "RAWW0000",
    "VERTEXCOLOR",
    "EXTRAUVS0", "EXTRAUVS1", "EXTRAUVS2", "EXTRAUVS3",
    "MORPHTARGETS",
    "MORPHNAMES",
})

# 100 MB hard ceiling on a single chunk's payload — anything larger is almost
# certainly a corrupt/forged size word, not a legit huge mesh chunk.
_MAX_PSK_CHUNK_BYTES = 100 * 1024 * 1024


def _decode_material_name(raw: bytes) -> str:
    """Decode a 64-byte material name slot from a PSK MATT chunk.

    Tries UTF-8 strict, falls back to cp1252 (Windows default for older UE
    exports), and finally ASCII with replacement characters. Logs a debug
    message when fallback fires so localized-game weirdness is visible.
    """
    raw = raw[:64].split(b"\x00", 1)[0]
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        decoded = raw.decode("cp1252")
        log.debug("PSK material name decoded via cp1252 fallback: %r", decoded)
        return decoded
    except UnicodeDecodeError:
        decoded = raw.decode("ascii", errors="replace")
        log.debug("PSK material name decoded via ASCII-replace: %r", decoded)
        return decoded


def _extract_psk_materials(psk_path: Path) -> tuple[list[str], bool]:
    """Read material names from the MATT0000 chunk in a PSK/PSKX file.

    Returns ``(names, ok)`` where ``ok=False`` signals that the file looks
    truncated or otherwise malformed — distinct from "well-formed file with no
    materials" (returns ``([], True)``). Callers can surface this as a
    ``scan_failed`` status instead of silently classifying it as "no_materials".
    """
    try:
        data = psk_path.read_bytes()
    except (OSError, IOError) as e:
        log.warning("Could not read PSK file %s: %s", psk_path, e)
        return [], False

    names: list[str] = []
    offset = 0
    size = len(data)
    while offset < size:
        if offset + 32 > size:
            # Trailing garbage shorter than a chunk header is malformed.
            log.warning(
                "PSK file %s truncated mid-header at offset %d/%d",
                psk_path, offset, size,
            )
            return names, False
        chunk_id = data[offset:offset + 20].split(b"\x00")[0]
        try:
            chunk_id_str = chunk_id.decode("ascii")
        except UnicodeDecodeError:
            log.warning(
                "PSK file %s has non-ASCII chunk id at offset %d",
                psk_path, offset,
            )
            return names, False
        dsize = struct.unpack_from("<I", data, offset + 24)[0]
        dcount = struct.unpack_from("<I", data, offset + 28)[0]

        # Bound-check the declared payload before we trust dsize * dcount.
        remaining = size - offset - 32
        # Guard against multiplication overflow / absurd values.
        if dsize > _MAX_PSK_CHUNK_BYTES or dcount > _MAX_PSK_CHUNK_BYTES:
            log.warning(
                "PSK file %s has implausible chunk dims at %s: dsize=%d dcount=%d",
                psk_path, chunk_id_str, dsize, dcount,
            )
            return names, False
        chunk_bytes = dsize * dcount
        if chunk_bytes > _MAX_PSK_CHUNK_BYTES or chunk_bytes > remaining:
            log.warning(
                "PSK file %s chunk %s overruns end (need %d, have %d)",
                psk_path, chunk_id_str, chunk_bytes, remaining,
            )
            return names, False

        if chunk_id_str == "MATT0000":
            if dsize < 64:
                log.warning(
                    "PSK file %s MATT chunk too small (dsize=%d)",
                    psk_path, dsize,
                )
                return names, False
            chunk_data = data[offset + 32: offset + 32 + chunk_bytes]
            for i in range(dcount):
                mat_data = chunk_data[i * dsize: (i + 1) * dsize]
                mat_name = _decode_material_name(mat_data)
                if mat_name:
                    names.append(mat_name)
            return names, True  # found MATT chunk, done

        if chunk_id_str not in _KNOWN_PSK_CHUNK_IDS:
            log.debug(
                "PSK file %s: unknown chunk id %r at offset %d (skipping)",
                psk_path, chunk_id_str, offset,
            )

        offset += 32 + chunk_bytes

    # Reached EOF cleanly without finding MATT — file is well-formed but has
    # no MATT chunk.
    return names, True


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _build_effective_presets(
    presets_data: dict,
    profile_overrides: Optional[dict],
    profile_preset: Optional[str],
    fallback_enabled: bool,
) -> dict:
    """Return a merged copy of *presets_data* with profile overrides on top.

    Per-material entries from *profile_overrides* completely replace any
    matching entry in the global ``material_overrides`` dict (whole-entry
    replace, not key-level merge — keeps the override-precedence rule
    obvious to the user). Stores the resolved fallback toggle under
    ``_auto_resolve_fallback`` so the resolver can read it without a
    signature change. Sets ``_default_preset`` to the profile's chosen
    preset so callers that don't explicitly pass ``preset_name`` still get
    the profile-selected default.
    """
    merged = dict(presets_data)
    global_overrides = dict(presets_data.get("material_overrides", {}))
    if profile_overrides:
        global_overrides.update(profile_overrides)
    merged["material_overrides"] = global_overrides
    merged["_auto_resolve_fallback"] = bool(fallback_enabled)
    if profile_preset:
        merged["_default_preset"] = profile_preset
    return merged


class AssetScanner:
    """Discovers and resolves all assets in a game folder."""

    def __init__(
        self,
        game_folder: str,
        presets_data: dict,
        sdk: Optional[EverythingSDK] = None,
        profile_overrides: Optional[dict] = None,
        profile_preset: Optional[str] = None,
        fallback_enabled: bool = True,
    ):
        self.game_folder = game_folder
        self.presets_data = _build_effective_presets(
            presets_data, profile_overrides, profile_preset, fallback_enabled
        )
        self.profile_preset = profile_preset or "default_pbr"
        self.sdk = sdk or get_sdk()
        self._cache: list[AssetEntry] = []
        self._cancelled = False

    @property
    def cached_results(self) -> list[AssetEntry]:
        return list(self._cache)

    def cancel(self):
        self._cancelled = True

    def seed_cache(self, entries: list[AssetEntry]):
        """Pre-populate the cache with previously resolved entries.

        When scan() runs, any mesh path already present will be reused
        instead of re-resolved.
        """
        self._cache = list(entries)

    def scan(self, progress_callback=None) -> list[AssetEntry]:
        """Run full scan. Returns list of AssetEntry.

        Args:
            progress_callback: Optional callable(current, total, message)
                for reporting progress to the GUI.
        """
        self._cancelled = False

        # Build lookup of already-cached entries by PSK path. Path strings
        # come back from Everything in their on-disk casing, but Windows is
        # case-insensitive — normalize to avoid duplicate cache entries when
        # the casing drifts between scans.
        existing = {
            os.path.normcase(str(e.psk_path)): e for e in self._cache
        }
        self._cache.clear()

        # 1. Find all supported mesh files (PSK/PSKX + GLB/FBX exports)
        log.info(
            "Searching for mesh files (psk/pskx/glb/fbx) in %s", self.game_folder
        )
        mesh_files = self.sdk.find_mesh_files(folder=self.game_folder)
        total = len(mesh_files)
        log.info("Found %d mesh files", total)

        if progress_callback:
            progress_callback(0, total, f"Found {total} meshes, resolving...")

        overrides = self.presets_data.get("material_overrides", {})

        for idx, mesh_path in enumerate(mesh_files):
            if self._cancelled:
                log.info("Scan cancelled at %d/%d", idx, total)
                if progress_callback:
                    progress_callback(idx, total, "Scan cancelled")
                break

            # Reuse cached entry if it was already resolved
            cached_entry = existing.get(os.path.normcase(str(mesh_path)))
            if cached_entry is not None and cached_entry.mesh_props_found:
                self._cache.append(cached_entry)
                if progress_callback:
                    progress_callback(idx, total, f"Cached: {mesh_path.stem}")
                continue

            asset_name = mesh_path.stem
            entry = AssetEntry(psk_path=mesh_path, name=asset_name)

            # Classify by folder path
            cat = classify(mesh_path, self.game_folder)
            entry.category = cat.category
            entry.subcategory = cat.subcategory

            if progress_callback:
                progress_callback(idx, total, f"Resolving: {asset_name}")

            # 2. Find companion mesh .props.txt
            self._resolve_mesh(entry, overrides)

            self._cache.append(entry)

        # Sort by name
        self._cache.sort(key=lambda a: a.name.lower())

        if not self._cancelled and progress_callback:
            progress_callback(total, total, "Scan complete")

        return list(self._cache)

    def _resolve_mesh(self, entry: AssetEntry, overrides: dict):
        """Find mesh props, parse materials, resolve textures."""
        # Find mesh props file
        props_files = self.sdk.find_props_file(
            entry.name, folder=self.game_folder
        )
        if not props_files:
            entry.mesh_props_found = False
            log.warning("No .props.txt found for %s", entry.name)
            return

        # Pick closest props file to the PSK
        props_path = props_files[0]
        if len(props_files) > 1:
            from core.texture_resolver import _pick_closest_path
            props_path = _pick_closest_path(props_files, entry.psk_path)

        try:
            mesh_props = parse_mesh_props_file(props_path)
        except Exception as e:
            log.error("Failed to parse %s: %s", props_path, e)
            entry.mesh_props_found = False
            return

        mat_refs = mesh_props.materials

        # Fallback: if props file has no materials, extract from PSK binary.
        # _extract_psk_materials now distinguishes "no materials" from "scan
        # failed" — surface the latter via mesh_props_found=False so the GUI
        # shows scan_failed instead of no_materials.
        if not mat_refs:
            if entry.psk_path.suffix.lower() not in (".psk", ".pskx"):
                # GLB/FBX inputs have no PSK chunk structure to mine; the
                # props material list is the only material source there
                # (CUE4Parse GLBs are untextured but keep their slot names).
                # Leave the entry's material list empty — its status becomes
                # "no_materials", never a bogus "scan_failed".
                log.info(
                    "Props for %s has no materials and its %s input has no "
                    "PSK binary to extract from",
                    entry.name, entry.psk_path.suffix,
                )
                return
            psk_mat_names, ok = _extract_psk_materials(entry.psk_path)
            if not ok:
                log.warning(
                    "PSK binary parse failed for %s — leaving as scan_failed",
                    entry.psk_path,
                )
                entry.mesh_props_found = False
                return
            if psk_mat_names:
                log.info(
                    "Props for %s has no materials; extracted %d from PSK binary",
                    entry.name, len(psk_mat_names),
                )
                mat_refs = [
                    MaterialRef(
                        slot_name=name,
                        material_name=name,
                        asset_path="",
                    )
                    for name in psk_mat_names
                ]

        # 3. For each material, find its props and resolve textures
        for mat_ref in mat_refs:
            mat_entry = self._resolve_material(mat_ref, entry.psk_path, overrides)
            # If material has no textures but has a parent, trace the chain
            if not mat_entry.textures and mat_entry.parent_name:
                self._resolve_parent_chain(mat_entry, entry.psk_path, overrides)
            entry.materials.append(mat_entry)
            entry.total_textures += len(mat_entry.textures) + len(mat_entry.unresolved)
            entry.missing_textures += len(mat_entry.unresolved)

    def _resolve_material(
        self, mat_ref: MaterialRef, psk_path: Path, overrides: dict
    ) -> MaterialEntry:
        """Resolve a single material's textures."""
        mat_entry = MaterialEntry(
            slot_name=mat_ref.slot_name,
            material_name=mat_ref.material_name,
        )

        # Check BSDF overrides
        if mat_ref.material_name in overrides:
            mat_entry.bsdf_overrides = overrides[mat_ref.material_name].get(
                "bsdf_overrides", {}
            )

        # Find material .props.txt
        mat_props_files = self.sdk.find_props_file(
            mat_ref.material_name, folder=self.game_folder
        )
        if not mat_props_files:
            mat_entry.props_found = False
            log.warning(
                "No .props.txt for material %s", mat_ref.material_name
            )
            return mat_entry

        mat_props_path = mat_props_files[0]
        if len(mat_props_files) > 1:
            from core.texture_resolver import _pick_closest_path
            mat_props_path = _pick_closest_path(mat_props_files, psk_path)

        try:
            mat_props = parse_material_props_file(mat_props_path)
        except Exception as e:
            log.error("Failed to parse %s: %s", mat_props_path, e)
            mat_entry.props_found = False
            return mat_entry

        # Store parent reference for chain traversal
        mat_entry.parent_name = mat_props.parent_name

        # Resolve textures.
        # A texture name can be bound to multiple parameters in one material
        # (e.g. the same image used as both BaseColor and Diffuse). Collect
        # them all so the resolver can pick the best param_name for the slot.
        tex_names = [t.texture_name for t in mat_props.textures]
        param_name_map: dict[str, list[str]] = {}
        for t in mat_props.textures:
            if t.param_name:
                param_name_map.setdefault(t.texture_name, []).append(t.param_name)
        if not tex_names:
            # Even with no textures, store color tints/scalar params if available
            if mat_props.color_tints:
                mat_entry.color_tints = mat_props.color_tints
            if mat_props.scalar_params:
                mat_entry.scalar_params = mat_props.scalar_params
            return mat_entry

        resolution = resolve_textures(
            texture_names=tex_names,
            presets_data=self.presets_data,
            sdk=self.sdk,
            preset_name=self.profile_preset,
            material_name=mat_ref.material_name,
            reference_path=psk_path,
            game_folder=self.game_folder,
            param_name_map=param_name_map,
        )

        mat_entry.textures = resolution.resolved
        mat_entry.unresolved = resolution.unresolved
        mat_entry.preset_used = resolution.preset_used
        mat_entry.keyword_fallback_used = list(resolution.keyword_fallback_used)

        # Store color tints and scalar params from material props
        if mat_props.color_tints:
            mat_entry.color_tints = mat_props.color_tints
        if mat_props.scalar_params:
            mat_entry.scalar_params = mat_props.scalar_params

        return mat_entry

    def _resolve_parent_chain(
        self, mat_entry: MaterialEntry, psk_path: Path, overrides: dict
    ):
        """Trace parent material chain to find textures for a textureless material.

        Walks up the parent chain until we find textures, hit a cycle, or
        exceed ``MAX_PARENT_DEPTH``. Visited names are lower-cased so a
        case-mixed cycle (``MI_Foo`` -> ``mi_foo``) is detected as such.
        """
        visited: set[str] = {mat_entry.material_name.lower()}
        child_color_tints = dict(mat_entry.color_tints)
        child_scalar_params = dict(mat_entry.scalar_params)
        current_parent = mat_entry.parent_name
        depth = 0

        while current_parent and current_parent.lower() not in visited:
            if depth >= MAX_PARENT_DEPTH:
                log.warning(
                    "Parent chain depth cap (%d) hit for %s; chain so far: %s",
                    MAX_PARENT_DEPTH,
                    mat_entry.material_name,
                    " -> ".join(sorted(visited)),
                )
                return
            visited.add(current_parent.lower())
            depth += 1
            log.info(
                "Material %s has no textures, tracing parent: %s",
                mat_entry.material_name, current_parent,
            )

            parent_props_files = self.sdk.find_props_file(
                current_parent, folder=self.game_folder
            )
            if not parent_props_files:
                log.warning(
                    "Parent material %s .props.txt not found (chain from %s)",
                    current_parent, mat_entry.material_name,
                )
                break

            parent_props_path = parent_props_files[0]
            if len(parent_props_files) > 1:
                from core.texture_resolver import _pick_closest_path
                parent_props_path = _pick_closest_path(
                    parent_props_files, psk_path
                )

            try:
                parent_props = parse_material_props_file(parent_props_path)
            except Exception as e:
                log.error(
                    "Failed to parse parent %s: %s", parent_props_path, e
                )
                break

            tex_names = [t.texture_name for t in parent_props.textures]
            param_name_map: dict[str, list[str]] = {}
            for t in parent_props.textures:
                if t.param_name:
                    param_name_map.setdefault(t.texture_name, []).append(
                        t.param_name
                    )

            if tex_names:
                # Found textures in this ancestor — resolve them
                resolution = resolve_textures(
                    texture_names=tex_names,
                    presets_data=self.presets_data,
                    sdk=self.sdk,
                    preset_name=self.profile_preset,
                    material_name=current_parent,
                    reference_path=psk_path,
                    game_folder=self.game_folder,
                    param_name_map=param_name_map,
                )
                mat_entry.textures = resolution.resolved
                mat_entry.unresolved = resolution.unresolved
                mat_entry.preset_used = resolution.preset_used
                mat_entry.keyword_fallback_used = list(resolution.keyword_fallback_used)

                # Merge color_tints: start with parent, overlay child values
                merged_tints = dict(parent_props.color_tints)
                merged_tints.update(child_color_tints)
                mat_entry.color_tints = merged_tints

                # Merge scalar_params: start with parent, overlay child values
                merged_scalars = dict(parent_props.scalar_params)
                merged_scalars.update(child_scalar_params)
                mat_entry.scalar_params = merged_scalars

                log.info(
                    "Found %d textures in ancestor %s for material %s",
                    len(resolution.resolved), current_parent,
                    mat_entry.material_name,
                )
                return

            # No textures here either — collect params and continue up
            # Parent params are base; child values already collected override them
            for k, v in parent_props.color_tints.items():
                if k not in child_color_tints:
                    child_color_tints[k] = v
            for k, v in parent_props.scalar_params.items():
                if k not in child_scalar_params:
                    child_scalar_params[k] = v

            current_parent = parent_props.parent_name

        log.warning(
            "Parent chain exhausted for %s — no textures found",
            mat_entry.material_name,
        )

    def resolve_entry(self, entry: AssetEntry) -> AssetEntry:
        """Re-resolve a single entry from scratch (keeps psk_path/category).

        Useful for re-scanning incomplete entries that were missing props or
        textures without re-running the full scan.
        """
        overrides = self.presets_data.get("material_overrides", {})
        entry.materials.clear()
        entry.mesh_props_found = True
        entry.total_textures = 0
        entry.missing_textures = 0
        self._resolve_mesh(entry, overrides)
        return entry


# ---------------------------------------------------------------------------
# Scan cache persistence
# ---------------------------------------------------------------------------

_CACHE_VERSION = 1
_CACHE_BAK_RETENTION_DAYS = 30
from _base import base_dir as _base_dir

_DEFAULT_CACHE_DIR = _base_dir() / "cache"


def _get_cache_path(game_folder: str) -> Path:
    """Return cache file path derived from the game folder."""
    # Use a hash of the game folder to support multiple game projects
    import hashlib
    folder_hash = hashlib.md5(game_folder.encode()).hexdigest()[:12]
    return _DEFAULT_CACHE_DIR / f"scan_{folder_hash}.json"


def sweep_old_cache_backups(
    cache_dir: Optional[Path] = None,
    retention_days: int = _CACHE_BAK_RETENTION_DAYS,
) -> int:
    """Delete ``scan_*.json.bak.*`` backups older than *retention_days*.

    Run this on app startup so a long-running install never accumulates more
    than a month of stale rename backups. Returns the count actually deleted
    (best-effort — silent on individual delete failures).
    """
    cache_dir = cache_dir or _DEFAULT_CACHE_DIR
    if not cache_dir.is_dir():
        return 0
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for path in cache_dir.glob("scan_*.json.bak.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as e:
            log.debug("Could not sweep cache backup %s: %s", path, e)
    if deleted:
        log.info("Swept %d stale scan-cache backup(s) older than %d days",
                 deleted, retention_days)
    return deleted


def _asset_to_dict(entry: AssetEntry) -> dict:
    """Serialize an AssetEntry to a plain dict."""
    materials = []
    for m in entry.materials:
        materials.append({
            "slot_name": m.slot_name,
            "material_name": m.material_name,
            "textures": [
                {
                    "slot": t.slot,
                    "texture_name": t.texture_name,
                    "path": str(t.path),
                    "colorspace": t.colorspace,
                    "wiring": t.wiring,
                }
                for t in m.textures
            ],
            "unresolved": [
                {"texture_name": u.texture_name, "reason": u.reason}
                for u in m.unresolved
            ],
            "props_found": m.props_found,
            "preset_used": m.preset_used,
            "bsdf_overrides": m.bsdf_overrides,
            "color_tints": m.color_tints,
            "scalar_params": m.scalar_params,
            "parent_name": m.parent_name,
            "keyword_fallback_used": list(m.keyword_fallback_used),
        })
    return {
        "psk_path": str(entry.psk_path),
        "name": entry.name,
        "materials": materials,
        "mesh_props_found": entry.mesh_props_found,
        "total_textures": entry.total_textures,
        "missing_textures": entry.missing_textures,
        "category": entry.category,
        "subcategory": entry.subcategory,
        "blend_path": str(entry.blend_path) if entry.blend_path else None,
        "processed": entry.processed,
    }


def _dict_to_asset(d: dict) -> AssetEntry:
    """Deserialize a plain dict back to an AssetEntry."""
    materials = []
    for md in d.get("materials", []):
        textures = [
            ResolvedTexture(
                slot=t["slot"],
                texture_name=t["texture_name"],
                path=Path(t["path"]),
                colorspace=t["colorspace"],
                wiring=t.get("wiring", {}),
            )
            for t in md.get("textures", [])
        ]
        unresolved = [
            UnresolvedTexture(
                texture_name=u["texture_name"],
                reason=u["reason"],
            )
            for u in md.get("unresolved", [])
        ]
        materials.append(MaterialEntry(
            slot_name=md["slot_name"],
            material_name=md["material_name"],
            textures=textures,
            unresolved=unresolved,
            props_found=md.get("props_found", True),
            preset_used=md.get("preset_used", "default_pbr"),
            bsdf_overrides=md.get("bsdf_overrides", {}),
            color_tints=md.get("color_tints", {}),
            scalar_params=md.get("scalar_params", {}),
            parent_name=md.get("parent_name", ""),
            keyword_fallback_used=list(md.get("keyword_fallback_used", [])),
        ))

    bp = d.get("blend_path")
    blend_path = Path(bp) if bp else None
    processed = d.get("processed", False)
    # Auto-detect processed state from the on-disk .blend so it stays in sync
    # both ways: flip True if the blend showed up out-of-band (e.g. user
    # manually exported), and flip False if it was deleted while we weren't
    # looking. Asymmetric auto-detect would let stale processed=True linger
    # forever after a cleanup.
    if blend_path is not None:
        on_disk = blend_path.is_file()
        if on_disk and not processed:
            processed = True
        elif not on_disk and processed:
            processed = False
    return AssetEntry(
        psk_path=Path(d["psk_path"]),
        name=d["name"],
        materials=materials,
        mesh_props_found=d.get("mesh_props_found", True),
        total_textures=d.get("total_textures", 0),
        missing_textures=d.get("missing_textures", 0),
        category=d.get("category", "Uncategorized"),
        subcategory=d.get("subcategory", "General"),
        blend_path=blend_path,
        processed=processed,
    )


def save_scan_cache(assets: list[AssetEntry], game_folder: str) -> Path:
    """Save scan results to a JSON cache file. Returns the cache path."""
    cache_path = _get_cache_path(game_folder)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": _CACHE_VERSION,
        "game_folder": game_folder,
        "timestamp": time.time(),
        "asset_count": len(assets),
        "assets": [_asset_to_dict(a) for a in assets],
    }

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))

    log.info("Saved scan cache: %d assets → %s", len(assets), cache_path)
    return cache_path


def load_scan_cache(game_folder: str) -> tuple[list[AssetEntry], float] | None:
    """Load cached scan results if available.

    On version mismatch, the old cache file is renamed to
    ``scan_<hash>.json.bak.<unix_ts>`` so the next save can write a clean
    canonical file without losing the old payload. Backups are pruned by
    :func:`sweep_old_cache_backups` on app startup.

    Returns:
        (assets, timestamp) tuple, or None if no usable cache exists.
    """
    cache_path = _get_cache_path(game_folder)
    if not cache_path.is_file():
        return None

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        version_found = data.get("version")
        if version_found != _CACHE_VERSION:
            log.warning(
                "Scan cache version mismatch at %s (found=%r, expected=%r); "
                "renaming and ignoring",
                cache_path, version_found, _CACHE_VERSION,
            )
            try:
                bak = cache_path.with_suffix(
                    f".json.bak.{int(time.time())}"
                )
                cache_path.rename(bak)
                log.info("Old cache renamed to %s", bak)
            except OSError as e:
                log.warning("Could not rename old cache file: %s", e)
            return None

        if data.get("game_folder") != game_folder:
            return None

        assets = [_dict_to_asset(d) for d in data.get("assets", [])]
        timestamp = data.get("timestamp", 0.0)
        log.info("Loaded scan cache: %d assets from %s", len(assets), cache_path)
        return assets, timestamp
    except Exception as e:
        log.error("Failed to load scan cache: %s", e)
        return None
