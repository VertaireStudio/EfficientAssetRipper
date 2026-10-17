"""Persistent application settings via QSettings."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from _base import base_dir

log = logging.getLogger(__name__)

_DEFAULTS = {
    "game_folder": "",
    "blender_exe": "",
    "output_dir": "",
    "everything_dll": "",
    "psk_addon_name": "bl_ext.blender_org.io_scene_psk_psa",
    "timeout_seconds": 120,
    "presets_path": str(base_dir() / "data" / "texture_presets.json"),
    "presets_path_confirmed_external": "",
    "cue4parse_cli": "",
    "unpack_output_dir": "",
    "aes_keys": "[]",
    "unpack_ue_version": "GAME_UE5_4",
    "export_texture_format": "png",
    "export_audio_format": "wav",
    "active_profile": "Default",
    "color_scheme": "Dusk",
    "custom_schemes": "{}",
    "ui_scale": 100,
    "use_known_games_map": False,
    "setup_complete": "",
}

_qs = QSettings("EfficientAssetRipper", "EfficientAssetRipper")

# Migrate stale addon name from old default
_OLD_ADDON = "io_import_scene_unreal_psa_psk_280"
if _qs.value("psk_addon_name", "") == _OLD_ADDON:
    _qs.setValue("psk_addon_name", _DEFAULTS["psk_addon_name"])


def get(key: str) -> str:
    return str(_qs.value(key, _DEFAULTS.get(key, "")))


def get_int(key: str) -> int:
    return int(_qs.value(key, _DEFAULTS.get(key, 0)))


def get_raw(key: str) -> Any:
    """Return the QSettings value preserving its native type.

    Use this when callers need a list/bool/int and should not have a string
    representation forced onto them. ``get`` continues to apply ``str(...)``.
    """
    return _qs.value(key, _DEFAULTS.get(key, ""))


def set(key: str, value) -> None:  # noqa: A001
    _qs.setValue(key, value)


def get_presets_path() -> Path:
    """Return a usable presets-JSON path, falling back to the bundled file.

    If the stored value points at a path that no longer exists (common after
    moving the install — e.g. the project was previously synced from Google
    Drive on G:\\), we silently fall back to ``<app>/data/texture_presets.json``
    instead of letting the broken path leak into Settings, the resolver, and
    the Verify-Setup test. Same pattern as ``get_cue4parse_cli`` below.
    """
    bundled = base_dir() / "data" / "texture_presets.json"
    p = get("presets_path")
    if p:
        try:
            if Path(p).is_file():
                return Path(p)
        except OSError:
            pass
    return bundled


def get_cue4parse_cli() -> str:
    """Return a usable CUE4ParseCLI path, falling back to the bundled binary.

    Resolution order:
      1. The user-configured ``cue4parse_cli`` path, if it exists.
      2. The bundled binary at ``<app>/cue4parse_cli/bin/publish/``, whose
         name is platform-dependent (``CUE4ParseCLI.exe`` on Windows,
         ``CUE4ParseCLI`` elsewhere).
      3. The configured value as-is, so the caller can produce its own
         "not found" error pointing at what the user actually set.

    This shields users whose stored path went stale (e.g. moved repos out
    of a Drive folder) from having to re-pick the CLI in Settings.
    """
    from core.tools_detect import bundled_cue4parse_cli
    configured = get("cue4parse_cli")
    if configured:
        try:
            if Path(configured).is_file():
                return configured
        except OSError:
            pass
    bundled = bundled_cue4parse_cli()
    if bundled.is_file():
        return str(bundled)
    return configured


def is_presets_path_safe(path: Path | str) -> bool:
    """Return True if *path* is the bundled presets file (no prompt needed)."""
    bundled_dir = (base_dir() / "data").resolve()
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(bundled_dir)
    except ValueError:
        return False
    return True


def _validate_presets_shape(payload: object) -> bool:
    """Lightweight schema check for ``texture_presets.json``.

    The full structure has many optional fields; we only insist on the
    invariants the resolver actually depends on:

    - top level is a dict
    - ``presets`` exists and is a dict
    - every preset is a dict with a ``texture_slots`` dict
    - every slot is a dict (suffix/param_name lists are optional)
    """
    if not isinstance(payload, dict):
        return False
    presets = payload.get("presets")
    if not isinstance(presets, dict):
        return False
    for preset in presets.values():
        if not isinstance(preset, dict):
            return False
        slots = preset.get("texture_slots")
        if not isinstance(slots, dict):
            return False
        for slot in slots.values():
            if not isinstance(slot, dict):
                return False
    return True


def _load_bundled_presets() -> dict:
    bundled = base_dir() / "data" / "texture_presets.json"
    with open(bundled, "r", encoding="utf-8") as f:
        return json.load(f)


def load_presets() -> dict:
    """Load presets from the configured path, falling back to bundled defaults.

    Falls back when the file is missing, unparseable, or fails the shape
    check — never raises out to the caller. The fallback path keeps the
    app usable even if a user hand-edits ``texture_presets.json`` into
    something invalid.
    """
    path = get_presets_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        log.warning("texture_presets.json missing at %s; using bundled defaults", path)
        return _load_bundled_presets()
    except json.JSONDecodeError as e:
        log.error("texture_presets.json is not valid JSON (%s); falling back", e)
        return _load_bundled_presets()
    except OSError as e:
        log.error("texture_presets.json unreadable (%s); falling back", e)
        return _load_bundled_presets()

    if not _validate_presets_shape(payload):
        log.error(
            "texture_presets.json at %s failed shape validation; falling back",
            path,
        )
        return _load_bundled_presets()
    return payload


def all_keys() -> list[str]:
    return list(_DEFAULTS.keys())
