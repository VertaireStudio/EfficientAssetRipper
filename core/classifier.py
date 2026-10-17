"""Classify PSK/PSKX assets into categories based on their folder path.

Works with any UE4/5-exported game folder. Uses the top-level and secondary
folder segments to assign a human-readable category and subcategory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass


def _split_any_path(candidate) -> tuple[str, ...]:
    """Split a path string into parts despite platform separator differences.

    PSK paths and game folders flow in from profiles, caches, and the CLI —
    a cache written on Windows may still carry backslash separators when
    consumed on Linux (and vice versa in the tests). Normalising to forward
    slashes before splitting keeps classification platform-independent.
    """
    raw = str(candidate).replace("\\", "/")
    return tuple(p for p in raw.split("/") if p)
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category mapping — top-level folder → category
# Order matters: first match wins
# ---------------------------------------------------------------------------

# Exact top-level folder → category
_TOP_LEVEL_MAP: dict[str, str] = {
    "characters":       "Characters",
    "items":            "Items",
    "vehicles":         "Vehicles",
    "levels":           "Levels",
    "models":           "Models",
    "modelsr8":         "Models",
    "meshes":           "Environment",
    "effects":          "Effects",
    "worldinteracts":   "Interactables",
    "weapons":          "Weapons",
    "materials":        "Materials",
    "materialsr8":      "Materials",
    "cinematics":       "Cinematics",
    "audio":            "Audio",
    "ui":               "UI",
    "lighting":         "Lighting",
    "foliage":          "Foliage",
    "techart":          "Tech Art",
    "houdiniengine":    "Procedural",
    "narrative":        "Narrative",
    "globaldata":       "Global",
    "developers":       "Developer",
}

# Keyword patterns that override when found anywhere in the relative path
_KEYWORD_OVERRIDES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bweapon", re.IGNORECASE),        "Weapons"),
    (re.compile(r"\bvehicle", re.IGNORECASE),       "Vehicles"),
    (re.compile(r"\bfoliage", re.IGNORECASE),       "Foliage"),
    (re.compile(r"\bcharacter", re.IGNORECASE),     "Characters"),
]


@dataclass
class AssetCategory:
    """Category + subcategory for an asset."""
    category: str       # e.g. "Characters"
    subcategory: str    # e.g. "B1Droid"  (second-level folder or "General")
    reason: str = ""    # diagnostic — set when category is "Uncategorized"

    @property
    def display(self) -> str:
        return f"{self.category} / {self.subcategory}"


# Common UE content roots — used as fallback markers when the configured
# game_folder doesn't line up with the PSK path. Lowercased for matching.
_UE_PATH_MARKERS = ("content", "game")


def _relative_parts(psk_path: Path, game_folder: str) -> tuple[tuple[str, ...], str]:
    """Compute path parts of *psk_path* relative to *game_folder*.

    Returns ``(parts, reason)``. ``reason`` is empty on success, or a short
    diagnostic string describing why the relative path could not be derived.
    Walks three strategies before giving up:

    1. ``Path.relative_to`` (case-sensitive, fastest path).
    2. Case-insensitive parts-prefix walk — handles the Windows situation
       where the configured ``game_folder`` and the on-disk PSK path differ
       only in casing.
    3. UE convention markers (``Content``/``Game``) — slice from the first
       occurrence in the PSK path. Catches profiles that point at the wrong
       sibling directory but still share a recognisable UE root.
    """
    try:
        relative = Path(str(psk_path).replace("\\", "/")).relative_to(
            Path(str(game_folder).replace("\\", "/"))
        )
        return relative.parts, ""
    except ValueError:
        pass

    psk_parts = _split_any_path(psk_path)
    if not psk_parts:
        return (), "empty_psk_path"

    psk_lower = tuple(p.lower() for p in psk_parts)
    gf_parts = _split_any_path(game_folder)
    gf_lower = tuple(p.lower() for p in gf_parts)

    if gf_lower and len(gf_lower) <= len(psk_lower):
        # Look for the configured game_folder anywhere as a contiguous run of
        # parts (case-insensitive). Picks the earliest match so the slice
        # represents the most-of-the-tree under it.
        for start in range(0, len(psk_lower) - len(gf_lower) + 1):
            if psk_lower[start:start + len(gf_lower)] == gf_lower:
                return psk_parts[start + len(gf_lower):], ""

    # Marker fallback: if the user pointed game_folder somewhere odd but the
    # PSK clearly sits under a UE Content/Game root, classify from there.
    for marker in _UE_PATH_MARKERS:
        for idx, part in enumerate(psk_lower):
            if part == marker and idx + 1 < len(psk_parts):
                return psk_parts[idx + 1:], ""

    return (), "path_not_under_game_folder"


def classify(psk_path: Path, game_folder: str) -> AssetCategory:
    """Classify an asset based on its path relative to the game folder.

    Args:
        psk_path: Absolute path to the PSK/PSKX file.
        game_folder: The root game content folder path.

    Returns:
        AssetCategory with category and subcategory strings. When the path
        cannot be located under the game folder, ``reason`` carries a short
        diagnostic the GUI can show next to the category.
    """
    parts, reason = _relative_parts(psk_path, game_folder)

    if not parts:
        log.warning(
            "classify: %s could not be made relative to game_folder %r (%s)",
            psk_path, game_folder, reason or "no_parts",
        )
        return AssetCategory("Uncategorized", "Unknown", reason=reason or "no_parts")

    # Slide the window forward to the first segment AFTER the UE
    # ``Content``/``Game`` marker. This handles layouts where the unpacker
    # preserves a game-name folder ahead of Content, e.g.
    # ``Obduction/Content/Avatars/...`` — without this, "Obduction" and
    # "Content" themselves would surface as top-level categories. We strip
    # only once (the first marker found) so a hypothetical nested
    # ``Content/Engine/Content/...`` doesn't lose mid-tree structure.
    _SKIP = {"game", "content"}
    for idx, part in enumerate(parts):
        if part.lower() in _SKIP and idx + 1 < len(parts):
            parts = parts[idx + 1:]
            break

    top = parts[0].lower()
    category = _TOP_LEVEL_MAP.get(top)

    # If no direct map, check keyword overrides on the full relative path
    if category is None:
        rel_str = "/".join(parts)
        for pattern, cat in _KEYWORD_OVERRIDES:
            if pattern.search(rel_str):
                category = cat
                break

    # Last resort: use the actual top-level folder name (preserving its
    # on-disk casing) as the category. UE games organise Content/ however
    # the studio wants — Obduction has Avatars/Skies/MergedMeshes/etc. that
    # don't match any curated alias. Falling through to a single "Other"
    # bucket made the picker useless on those titles. Better to mirror the
    # real folder structure and let the user see what's there.
    if category is None:
        category = parts[0]

    # Subcategory = second-level folder if available, otherwise "General"
    subcategory = parts[1] if len(parts) > 2 else "General"

    return AssetCategory(category=category, subcategory=subcategory)


def get_all_categories() -> list[str]:
    """Return all known category names in display order."""
    seen = set()
    ordered = []
    for cat in _TOP_LEVEL_MAP.values():
        if cat not in seen:
            seen.add(cat)
            ordered.append(cat)
    ordered.extend(["Other", "Uncategorized"])
    return ordered
