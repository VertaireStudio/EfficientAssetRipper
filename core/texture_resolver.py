"""Classify textures by suffix and resolve them to on-disk TGA paths.

Uses the wiring presets from texture_presets.json and the Everything SDK
to turn a list of texture names (from a material's props.txt) into a dict
mapping each slot to an absolute file path + colorspace.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.everything import EverythingSDK

log = logging.getLogger(__name__)


# Cache of compiled `regex_suffixes` patterns. Keyed by the raw user pattern
# string; value is the compiled re.Pattern, or None if compilation failed (so
# we don't re-warn on every call). Patterns are wrapped as ``(?:USER)\Z`` at
# compile time so a top-level alternation gets anchored cleanly.
_REGEX_SUFFIX_CACHE: dict[str, Optional[re.Pattern[str]]] = {}


def _compile_regex_suffix(pattern: str) -> Optional[re.Pattern[str]]:
    """Return a cached compiled pattern for *pattern*, or None on bad regex.

    The first failure is logged at WARNING; subsequent calls return None
    silently. Patterns are wrapped as ``(?:<pattern>)\\Z`` so that a top-level
    ``|`` alternation gets the end-anchor applied to every branch and not just
    the last one. Compiled with ``re.IGNORECASE`` so authors can write either
    case in their patterns regardless of how the texture name is normalised.
    """
    if pattern in _REGEX_SUFFIX_CACHE:
        return _REGEX_SUFFIX_CACHE[pattern]
    try:
        compiled = re.compile(f"(?:{pattern})\\Z", re.IGNORECASE)
    except re.error as e:
        log.warning(
            "Skipping invalid regex_suffix %r: %s", pattern, e,
        )
        _REGEX_SUFFIX_CACHE[pattern] = None
        return None
    _REGEX_SUFFIX_CACHE[pattern] = compiled
    return compiled


@dataclass
class ResolvedTexture:
    """A texture that has been classified and located on disk."""
    slot: str              # e.g. "base_color", "orm", "normal"
    texture_name: str      # original name from props file
    path: Path             # absolute path to TGA on disk
    colorspace: str        # "sRGB" or "Non-Color"
    wiring: dict           # wiring spec from presets JSON


@dataclass
class UnresolvedTexture:
    """A texture that could not be classified or found on disk."""
    texture_name: str
    reason: str            # "no_matching_suffix" or "file_not_found"


@dataclass
class TextureResolution:
    """Full resolution result for one material."""
    resolved: list[ResolvedTexture]
    unresolved: list[UnresolvedTexture]
    preset_used: str
    keyword_fallback_used: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.keyword_fallback_used is None:
            self.keyword_fallback_used = []


def _pick_closest_path(candidates: list[Path], reference: Path) -> Path:
    """Pick the candidate sharing the longest common path prefix with reference.

    Ties are broken deterministically by ``(len(parts), str(c).lower())`` so
    that scan order or filesystem enumeration order does not change the
    result. Prefix comparison is separator-agnostic: cached Windows-style
    backslash paths must still prefix-match against POSIX reference paths
    (and vice versa), so paths are normalised to forward-slash parts.
    """
    if len(candidates) == 1:
        return candidates[0]

    ref_norm = str(reference).replace("\\", "/")
    ref_parts = tuple(p for p in ref_norm.split("/") if p)

    def norm_parts(p: Path) -> tuple[str, ...]:
        return tuple(part for part in str(p).replace("\\", "/").split("/") if part)

    # No reference to compare against — pick the shortest/lexicographically-first.
    if not ref_parts:
        return sorted(
            candidates, key=lambda c: (len(norm_parts(c)), str(c).lower())
        )[0]

    scored: list[tuple[int, Path]] = []
    for c in candidates:
        score = 0
        for a, b in zip(norm_parts(c), ref_parts):
            if a.lower() == b.lower():
                score += 1
            else:
                break
        scored.append((score, c))

    best_score = max(s for s, _ in scored)
    tied = [c for s, c in scored if s == best_score]
    if len(tied) == 1:
        return tied[0]
    return sorted(tied, key=lambda c: (len(norm_parts(c)), str(c).lower()))[0]


def classify_texture(
    texture_name: str,
    texture_slots: dict,
    param_name: str = "",
    priority_order: Optional[list[str]] = None,
) -> Optional[tuple[str, dict]]:
    """Determine which slot a texture name belongs to.

    Suffix matching wins by *longest matching suffix* across all slots, so
    `_ORM` always beats `_OR` regardless of dict iteration order. Within a
    slot, suffixes are also sorted by descending length for the same reason.
    Ties (equal-length suffixes in different slots) are broken by
    `priority_order`; slots not listed in `priority_order` fall through in
    stable dict order.

    The param_name fallback (e.g. ``BaseColor`` from ``TextureParameterValues``)
    runs only when no suffix matches. It walks slots in `priority_order` too.

    Returns (slot_name, slot_config) or None if nothing matches.
    """
    name_upper = texture_name.rstrip("_").upper()

    # Build the slot ordering once: priority_order entries first, then any
    # unlisted slots in stable dict order.
    ordered_slots: list[str] = []
    if priority_order:
        for s in priority_order:
            if s in texture_slots and s not in ordered_slots:
                ordered_slots.append(s)
    for s in texture_slots:
        if s not in ordered_slots:
            ordered_slots.append(s)
    slot_priority = {name: idx for idx, name in enumerate(ordered_slots)}

    # Pass 1: longest matching suffix wins; tiebreak by priority_order.
    # Both literal and regex suffixes feed the same `best` tuple so a longer
    # regex match in slot X correctly beats a shorter literal match in slot Y.
    best: Optional[tuple[int, int, str, dict]] = None
    for slot_name, slot_cfg in texture_slots.items():
        prio = slot_priority.get(slot_name, 1 << 30)

        # Literal suffixes — longest first within the slot.
        suffixes = sorted(
            slot_cfg.get("suffixes", []), key=lambda s: -len(s)
        )
        for suffix in suffixes:
            if not suffix:
                continue
            if name_upper.endswith(suffix.upper()):
                key = (-len(suffix), prio)
                if best is None or key < (best[0], best[1]):
                    best = (key[0], key[1], slot_name, slot_cfg)
                break  # only the longest literal suffix in this slot matters

        # Regex suffixes — every pattern is tried; the longest match in this
        # slot competes against `best` from the literal pass.
        for pattern in slot_cfg.get("regex_suffixes", []) or []:
            compiled = _compile_regex_suffix(pattern)
            if compiled is None:
                continue
            m = compiled.search(name_upper)
            if m is None:
                continue
            matched_len = m.end() - m.start()
            key = (-matched_len, prio)
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], slot_name, slot_cfg)
    if best is not None:
        return best[2], best[3]

    # Pass 2: param_name matching (e.g. ParameterInfo Name = "BaseColor")
    if param_name:
        for slot_name in ordered_slots:
            slot_cfg = texture_slots.get(slot_name)
            if slot_cfg is None:
                continue
            for pn in slot_cfg.get("param_names", []):
                if param_name == pn:
                    return slot_name, slot_cfg
    return None


def resolve_textures(
    texture_names: list[str],
    presets_data: dict,
    sdk: EverythingSDK,
    preset_name: str = "default_pbr",
    material_name: str = "",
    reference_path: Optional[Path] = None,
    game_folder: str = "",
    param_name_map: Optional[dict[str, list[str]] | dict[str, str]] = None,
) -> TextureResolution:
    """Resolve a list of texture names to classified, located files.

    Args:
        texture_names: Base names from material props (e.g. ["XWing_01_Droid_C", ...])
        presets_data: Full parsed texture_presets.json
        sdk: Everything SDK instance
        preset_name: Which preset to use
        material_name: Material name for checking overrides
        reference_path: Path of the source PSK, used for closest-match logic
        game_folder: Scope Everything searches to this folder
        param_name_map: Optional dict mapping ``texture_name`` to either a
            single ParameterInfo Name (legacy, ``str``) or to a list of names
            (current — UE materials often bind the same texture to several
            parameters and the right one for slot classification can be any of
            them).
    """
    resolved: list[ResolvedTexture] = []
    unresolved: list[UnresolvedTexture] = []

    # Normalize the param_name_map to texture_name -> list[str].
    norm_param_names: dict[str, list[str]] = {}
    if param_name_map:
        for tex_name, value in param_name_map.items():
            if isinstance(value, str):
                if value:
                    norm_param_names[tex_name] = [value]
            elif isinstance(value, (list, tuple)):
                names = [v for v in value if v]
                if names:
                    norm_param_names[tex_name] = list(names)

    # Global ignore list (exact name) and ignore patterns (substring)
    ignore_textures = set(
        n.upper() for n in presets_data.get("ignore_textures", [])
    )
    ignore_patterns = [
        p.upper() for p in presets_data.get("ignore_patterns", [])
    ]

    # Check for per-material override
    overrides = presets_data.get("material_overrides", {})
    if material_name in overrides:
        override = overrides[material_name]
        preset_name = override.get("preset", preset_name)

    preset = presets_data.get("presets", {}).get(preset_name)
    if not preset:
        for tn in texture_names:
            unresolved.append(UnresolvedTexture(tn, f"preset '{preset_name}' not found"))
        return TextureResolution(resolved, unresolved, preset_name)

    texture_slots = preset["texture_slots"]
    priority_order = preset.get("priority_order") or []

    # If override forces specific textures, apply those
    force_textures: dict[str, str] = {}
    if material_name in overrides:
        force_textures = overrides[material_name].get("force_textures", {})

    # Track which slots we've already filled
    filled_slots: set[str] = set()

    # First pass: resolve forced textures from overrides
    for slot_name, forced_name in force_textures.items():
        if slot_name in texture_slots:
            slot_cfg = texture_slots[slot_name]
            candidates = sdk.find_texture(forced_name, folder=game_folder)
            if candidates:
                chosen = _pick_closest_path(candidates, reference_path or Path())
                resolved.append(ResolvedTexture(
                    slot=slot_name,
                    texture_name=forced_name,
                    path=chosen,
                    colorspace=slot_cfg.get("colorspace", "sRGB"),
                    wiring=slot_cfg.get("wiring", {}),
                ))
                filled_slots.add(slot_name)
            else:
                unresolved.append(UnresolvedTexture(forced_name, "file_not_found"))

    # Second pass: classify and resolve remaining textures
    for tex_name in texture_names:
        # Skip globally ignored texture names (exact or substring)
        name_up = tex_name.upper()
        if name_up in ignore_textures:
            continue
        if any(pat in name_up for pat in ignore_patterns):
            continue

        pnames = norm_param_names.get(tex_name, [])
        result = _classify_with_param_names(
            tex_name, texture_slots, pnames, priority_order
        )
        if result is None:
            unresolved.append(UnresolvedTexture(tex_name, "no_matching_suffix"))
            continue

        slot_name, slot_cfg = result

        if slot_name in filled_slots:
            continue  # Override already filled this slot

        candidates = sdk.find_texture(tex_name, folder=game_folder)
        if not candidates:
            unresolved.append(UnresolvedTexture(tex_name, "file_not_found"))
            continue

        chosen = _pick_closest_path(candidates, reference_path or Path())
        resolved.append(ResolvedTexture(
            slot=slot_name,
            texture_name=tex_name,
            path=chosen,
            colorspace=slot_cfg.get("colorspace", "sRGB"),
            wiring=slot_cfg.get("wiring", {}),
        ))
        filled_slots.add(slot_name)

    keyword_fallback_used: list[str] = []
    fallback_enabled = bool(
        presets_data.get("_auto_resolve_fallback", True)
        and preset.get("enable_keyword_fallback", True)
    )
    # Only run the keyword fallback when the main pass produced nothing.
    # When suffix/param matching already resolved at least one slot, the
    # material has a non-trivial explicit spec — padding the empty slots from
    # a wide folder scan pulls in unrelated textures (Obduction-style master
    # materials are the canonical offender; the broad Content/ scan finds
    # `*_Roughness`/`*_METALLIC` from elsewhere in the game).
    if fallback_enabled and reference_path is not None and not resolved:
        _apply_keyword_fallback(
            resolved=resolved,
            unresolved=unresolved,
            filled_slots=filled_slots,
            texture_slots=texture_slots,
            sdk=sdk,
            reference_path=reference_path,
            keyword_fallback_used=keyword_fallback_used,
        )

    return TextureResolution(
        resolved, unresolved, preset_name, keyword_fallback_used
    )


# Ordered keyword lists per slot — longest/most-specific first so that
# ``basecolor`` always beats a shorter ``color`` match. The fallback only
# fires when suffix and param_name classification both produced nothing.
_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("base_color", ["basecolor", "base_color", "albedo", "diffuse", "_d_", "color"]),
    ("normal", ["normal", "_nrm", "_nor_", "_n_"]),
    ("roughness", ["roughness", "rough", "_r_"]),
    ("metallic", ["metallic", "metal", "_m_", "met"]),
    ("ao", ["ambientocclusion", "occlusion", "_ao", "ambient"]),
    ("specular", ["specular", "spec", "_s_"]),
    ("emissive", ["emissive", "emission", "glow", "_e_"]),
    ("alpha", ["opacity", "alpha", "transparency"]),
    ("height", ["height", "displacement", "disp"]),
]


def _keyword_classify(stem_lower: str) -> Optional[tuple[str, str, int]]:
    """Score *stem_lower* against ``_KEYWORD_RULES``.

    Returns ``(slot_name, matched_keyword, position)`` for the best match, or
    ``None`` if nothing matched. Position is the index in *stem_lower* where
    the keyword appears (later wins on tie — UE conventions put
    role-suffixes near the end of the filename).
    """
    best: Optional[tuple[int, int, str, str]] = None  # (-len, -position, slot, kw)
    for slot, keywords in _KEYWORD_RULES:
        for kw in keywords:
            idx = stem_lower.find(kw)
            if idx == -1:
                continue
            # Prefer longer keyword, then later position in the stem.
            key = (-len(kw), -idx)
            if best is None or key < (best[0], best[1]):
                best = (-len(kw), -idx, slot, kw)
            break  # only the first/longest hit per slot matters
    if best is None:
        return None
    return best[2], best[3], -best[1]


def _scan_folder_for_psk(reference_path: Path) -> Optional[str]:
    """Pick a folder to scan for textures, given the source PSK's path.

    Walks up to 4 levels and stops at the first ancestor that *is* or
    *contains* a ``Textures``/``Materials`` directory — UE projects
    typically keep textures one or two folders away from the mesh, often as
    a sibling. The returned folder is intended to be passed to a recursive
    ``find_textures_in_folder`` query, so siblings under the chosen
    ancestor are reachable. Falls back to the PSK's own parent for flat
    layouts.
    """
    if not reference_path or not reference_path.parts:
        return None

    siblings_of_interest = {"textures", "materials"}
    parent = reference_path.parent
    cur = parent
    for _ in range(4):
        if cur.name.lower() in siblings_of_interest:
            return str(cur)
        try:
            children = {p.name.lower() for p in cur.iterdir() if p.is_dir()}
        except (OSError, PermissionError):
            children = set()
        if children & siblings_of_interest:
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return str(parent)


def _apply_keyword_fallback(
    resolved: list[ResolvedTexture],
    unresolved: list[UnresolvedTexture],
    filled_slots: set[str],
    texture_slots: dict,
    sdk: EverythingSDK,
    reference_path: Optional[Path],
    keyword_fallback_used: list[str],
) -> None:
    """Fill any still-empty slots by scanning textures near the reference PSK.

    Mutates *resolved*, *filled_slots*, and *keyword_fallback_used* in place.
    Confident suffix/param matches always win — the fallback only writes to
    slots not already in *filled_slots*. Removes from *unresolved* any entry
    whose name now points at a found-by-fallback texture.
    """
    if reference_path is None:
        return

    # Skip slots without keyword rules in our table — leaves room for new
    # slot types to be added to texture_slots without surprising fallback
    # behavior.
    addressable = {slot for slot, _ in _KEYWORD_RULES} & set(texture_slots.keys())
    empty = addressable - filled_slots
    if not empty:
        return

    folder = _scan_folder_for_psk(reference_path)
    if not folder:
        return

    finder = getattr(sdk, "find_textures_in_folder", None)
    if finder is None:
        # Older test stubs may lack the new method — degrade silently.
        return
    candidates: list[Path] = list(finder(folder))
    if not candidates:
        return

    # Score every TGA in the folder.
    scored: dict[str, list[tuple[int, int, Path, str]]] = {}
    for path in candidates:
        match = _keyword_classify(path.stem.lower())
        if match is None:
            continue
        slot, kw, pos = match
        if slot not in empty:
            continue
        scored.setdefault(slot, []).append((-len(kw), -pos, path, kw))

    if not scored:
        return

    found_names: set[str] = set()
    for slot, entries in scored.items():
        entries.sort()  # smallest tuple first → longest keyword + latest position
        # Tie-break further by closeness to the reference PSK.
        top_score = (entries[0][0], entries[0][1])
        tied = [p for s1, s2, p, _ in entries if (s1, s2) == top_score]
        chosen = _pick_closest_path(tied, reference_path) if len(tied) > 1 else entries[0][2]
        slot_cfg = texture_slots[slot]
        resolved.append(ResolvedTexture(
            slot=slot,
            texture_name=chosen.stem,
            path=chosen,
            colorspace=slot_cfg.get("colorspace", "sRGB"),
            wiring=slot_cfg.get("wiring", {}),
        ))
        filled_slots.add(slot)
        keyword_fallback_used.append(slot)
        found_names.add(chosen.stem.lower())

    if found_names:
        # Demote any unresolved entries whose name now matches a found texture.
        unresolved[:] = [
            u for u in unresolved
            if u.texture_name.lower() not in found_names
        ]


def _classify_with_param_names(
    tex_name: str,
    texture_slots: dict,
    param_names: list[str],
    priority_order: list[str],
) -> Optional[tuple[str, dict]]:
    """Try suffix-based classification first, then walk all bound param_names.

    A texture can be bound to several material parameters at once (e.g. the
    same image used as both ``BaseColor`` and ``Diffuse``). The suffix
    classifier already handles the common case; the param_name fallback walks
    each bound name in turn so we don't accidentally drop the only signal we
    have for a textureless suffix like ``T_Foo`` bound to ``BaseColor``.
    """
    # Suffix-only first (no param_name) so we never let a stale bind override
    # an unambiguous suffix match.
    suffix_match = classify_texture(
        tex_name, texture_slots, priority_order=priority_order
    )
    if suffix_match is not None:
        return suffix_match

    seen: set[str] = set()
    for pname in param_names:
        if not pname or pname in seen:
            continue
        seen.add(pname)
        match = classify_texture(
            tex_name,
            texture_slots,
            param_name=pname,
            priority_order=priority_order,
        )
        if match is not None:
            return match
    return None
