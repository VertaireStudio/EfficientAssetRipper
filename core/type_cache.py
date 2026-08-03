"""Disk-backed cache of every package's export types in a mounted game.

Populated once after `init` succeeds (via the CLI's `scan_types` command),
keyed by a fingerprint of the game folder + UE version, and reused on
subsequent mounts so type-aware filtering and classification stay instant.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from _base import base_dir as _base_dir

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_DEFAULT_CACHE_DIR = _base_dir() / "cache"

# Cache keys are VFS paths, so a CUE4Parse upgrade that remaps the mount tree
# invalidates every entry. Bump on any such upgrade to orphan stale caches
# rather than serve paths the CLI can no longer resolve. Gen 2 = CUE4Parse
# 1.2.2.202607, which dropped the synthetic "<GameDir>" root for loose files.
_PARSER_GENERATION = 2


# ---------------------------------------------------------------------------
# Category taxonomy — single source of truth shared by the GUI filter UI
# and the row classifier.
# ---------------------------------------------------------------------------

MESH_EXPORT_TYPES = frozenset({"SkeletalMesh", "StaticMesh"})
TEXTURE_EXPORT_TYPES = frozenset({"Texture2D", "TextureCube", "Texture2DArray"})
AUDIO_EXPORT_TYPES = frozenset({"SoundWave"})
VIDEO_EXPORT_TYPES = frozenset({"FileMediaSource"})
MATERIAL_EXPORT_TYPES = frozenset({
    "Material", "MaterialInstance", "MaterialInstanceConstant", "MaterialInterface",
})
ANIMATION_EXPORT_TYPES = frozenset({
    "AnimSequence", "AnimMontage", "AnimComposite", "AnimBlueprint",
})

CATEGORY_MESH = "mesh"
CATEGORY_TEXTURE = "texture"
CATEGORY_AUDIO = "audio"
CATEGORY_VIDEO = "video"
CATEGORY_MATERIAL = "material"
CATEGORY_ANIMATION = "animation"
CATEGORY_OTHER = "other"

ALL_CATEGORIES: frozenset[str] = frozenset({
    CATEGORY_MESH,
    CATEGORY_TEXTURE,
    CATEGORY_AUDIO,
    CATEGORY_VIDEO,
    CATEGORY_MATERIAL,
    CATEGORY_ANIMATION,
    CATEGORY_OTHER,
})


def category_for_export_type(export_type: str) -> str:
    """Map a single UE class name to one of the seven categories."""
    if export_type in MESH_EXPORT_TYPES:
        return CATEGORY_MESH
    if export_type in TEXTURE_EXPORT_TYPES:
        return CATEGORY_TEXTURE
    if export_type in AUDIO_EXPORT_TYPES:
        return CATEGORY_AUDIO
    if export_type in VIDEO_EXPORT_TYPES:
        return CATEGORY_VIDEO
    if export_type in MATERIAL_EXPORT_TYPES:
        return CATEGORY_MATERIAL
    if export_type in ANIMATION_EXPORT_TYPES:
        return CATEGORY_ANIMATION
    return CATEGORY_OTHER


# Strings the CLI's heuristic ClassifyAssetType emits that aren't real UE
# class names but still belong in a recognizable category. Kept separate
# from *_EXPORT_TYPES so preview-menu logic in _classify_row (which gates
# on real export types from list_exports) doesn't accidentally treat a
# WwiseAsset wrapper package as a playable SoundWave.
_HEURISTIC_AUDIO_TYPES = frozenset({"Audio", "EncryptedAudio", "SoundBank",
                                    "WwiseAsset", "AkAudioEvent", "AkMediaAsset"})

# Heuristic catch for video-bearing UE classes. We only export
# FileMediaSource today, but PlatformMediaSource / StreamMediaSource /
# ImgMediaSource still belong in CATEGORY_VIDEO so the filter UI lights
# them up correctly. Classification is read-only here.
_HEURISTIC_VIDEO_TYPES = frozenset({
    "FileMediaSource", "PlatformMediaSource",
    "StreamMediaSource", "ImgMediaSource",
})


def category_for_asset_type(asset_type: str) -> str:
    """Map a CLI heuristic asset_type to a category.

    Like category_for_export_type, but also handles the CLI's non-UE-class
    strings (Audio/AkAudioEvent/etc.) that ClassifyAssetType emits for
    loose audio files and Wwise wrapper packages.
    """
    if asset_type in _HEURISTIC_AUDIO_TYPES:
        return CATEGORY_AUDIO
    if asset_type in _HEURISTIC_VIDEO_TYPES:
        return CATEGORY_VIDEO
    return category_for_export_type(asset_type)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def compute_fingerprint(game_folder: str, ue_version: str) -> str:
    """12-char hash of (normalized game folder, UE version, parser generation)."""
    norm = (game_folder or "").strip().rstrip("/\\").lower()
    payload = f"{norm}|{ue_version or ''}|g{_PARSER_GENERATION}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def cache_path_for(fingerprint: str, cache_dir: Optional[Path] = None) -> Path:
    base = cache_dir or _DEFAULT_CACHE_DIR
    return base / f"types_{fingerprint}.json"


@dataclass
class TypeCache:
    """In-memory map of {package_vfs_path → list[{name, export_type}]}."""

    entries: dict[str, list[dict]] = field(default_factory=dict)
    error_count: int = 0
    total_packages: int = 0

    def __post_init__(self) -> None:
        # Folder index: maps each folder VFS path to the union of categories
        # of all packages under it. Built explicitly via rebuild_folder_index().
        self._folder_categories: dict[str, frozenset[str]] = {}

    # ----- Mutation -------------------------------------------------------

    def add_batch(self, entries: Iterable[dict]) -> None:
        """Merge a batch produced by the CLI's `types_scan_batch` payload.

        Also incrementally updates `_folder_categories` so a live category
        filter sees folders light up as their packages stream in, instead of
        waiting for `rebuild_folder_index()` at scan completion.
        """
        for e in entries:
            path = e.get("path")
            if not path:
                continue
            exports = e.get("exports") or []
            normalized = [
                {"name": x.get("name", ""), "export_type": x.get("export_type", "")}
                for x in exports
            ]
            self.entries[path] = normalized

            # Propagate this package's categories to every ancestor folder.
            cats: set[str] = {category_for_export_type(n["export_type"])
                              for n in normalized if n["export_type"]}
            if not cats:
                cats = {CATEGORY_OTHER}
            parts = path.split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                existing = self._folder_categories.get(folder)
                if existing is None:
                    self._folder_categories[folder] = frozenset(cats)
                elif not cats.issubset(existing):
                    self._folder_categories[folder] = frozenset(existing | cats)

    def clear(self) -> None:
        self.entries.clear()
        self._folder_categories.clear()
        self.error_count = 0
        self.total_packages = 0

    # ----- Lookup ---------------------------------------------------------

    def types_for_package(self, vfs_path: str) -> Optional[list[dict]]:
        """Return cached `[{name, export_type}, ...]` for *vfs_path*, or None."""
        return self.entries.get(vfs_path)

    def export_types_for_package(self, vfs_path: str) -> set[str]:
        """Return the set of distinct export_type strings in this package."""
        exports = self.entries.get(vfs_path)
        if not exports:
            return set()
        return {x.get("export_type", "") for x in exports if x.get("export_type")}

    def categories_for_package(self, vfs_path: str) -> set[str]:
        """Return the set of categories the cached package belongs to.

        Empty set means: not in the cache (caller should treat as 'unknown
        — assume any category' until the scan completes).
        """
        types = self.export_types_for_package(vfs_path)
        return {category_for_export_type(t) for t in types}

    def rebuild_folder_index(self) -> None:
        """Build a folder-path → frozenset[category] index from current entries.

        Walk every cached package path and propagate its categories up to each
        ancestor folder. O(n × depth). Call once after scan completes or after
        loading from disk — not after every add_batch.
        """
        index: dict[str, set[str]] = {}
        for vfs_path, exports in self.entries.items():
            cats = {category_for_export_type(e.get("export_type", "")) for e in exports if e.get("export_type")}
            if not cats:
                cats = {CATEGORY_OTHER}
            parts = vfs_path.split("/")
            # Propagate to every ancestor folder (skip the file itself).
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder in index:
                    index[folder].update(cats)
                else:
                    index[folder] = set(cats)
        self._folder_categories = {k: frozenset(v) for k, v in index.items()}

    def categories_under_folder(self, folder_path: str) -> frozenset[str]:
        """Return the union of categories of all cached packages under *folder_path*.

        Returns an empty frozenset if the folder has no indexed data (caller
        should treat unknown folders as visible — don't hide what we're unsure of).
        """
        return self._folder_categories.get(folder_path, frozenset())

    # ----- Persistence ----------------------------------------------------

    def save(self, fingerprint: str, cache_dir: Optional[Path] = None) -> Path:
        path = cache_path_for(fingerprint, cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _CACHE_VERSION,
            "fingerprint": fingerprint,
            "timestamp": time.time(),
            "error_count": self.error_count,
            "total_packages": self.total_packages,
            "package_count": len(self.entries),
            "entries": self.entries,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        log.info("Saved type cache: %d packages → %s", len(self.entries), path)
        return path

    @classmethod
    def load(cls, fingerprint: str, cache_dir: Optional[Path] = None) -> Optional["TypeCache"]:
        path = cache_path_for(fingerprint, cache_dir)
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not read type cache %s: %s", path, e)
            return None

        if data.get("version") != _CACHE_VERSION:
            log.info(
                "Type cache version mismatch at %s (found=%r, expected=%r); ignoring",
                path, data.get("version"), _CACHE_VERSION,
            )
            return None

        cache = cls()
        cache.entries = dict(data.get("entries") or {})
        cache.error_count = int(data.get("error_count", 0))
        cache.total_packages = int(data.get("total_packages", 0))
        cache.rebuild_folder_index()
        log.info("Loaded type cache: %d packages from %s", len(cache.entries), path)
        return cache
