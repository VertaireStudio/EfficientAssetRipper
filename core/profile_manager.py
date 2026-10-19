"""Game profile management — CRUD operations on per-game JSON profiles."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from _base import base_dir

log = logging.getLogger(__name__)

_PROFILES_DIR = base_dir() / "profiles"


class ProfileLoadError(Exception):
    """Raised when a profile file exists but cannot be loaded.

    Distinct from ``FileNotFoundError`` (handled by callers as "create a new
    one") and from corrupt-JSON recovery (which the loader handles silently
    by backing up the bad file and returning empty defaults). This is for the
    cases the user must act on — file locked, permission denied, OS error.
    """

# Characters not allowed in profile (file) names on Windows
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')

# Reserved Windows device names (case-insensitive, with or without extension)
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Keys that live per-profile (everything else stays in global QSettings).
# Appearance keys (color_scheme / custom_schemes) deliberately stayed out —
# they are application Settings, not per-game config, and storing them here
# made theme changes flip every time the user switched profiles.
PROFILE_KEYS = {
    "game_dir",
    "ue_version",
    "aes_keys",
    "unpack_output_dir",
    "blender_output_dir",
    "mappings_path",
    "scan_cache_file",
    "mesh_processed",
    "auto_save_paths",
    "texture_preset",
    "material_overrides",
    "auto_resolve_fallback",
}

_EMPTY_PROFILE: dict = {
    "name": "",
    "game_dir": "",
    "ue_version": "GAME_UE5_4",
    "aes_keys": [],
    "unpack_output_dir": "",
    "blender_output_dir": "",
    "mappings_path": "",
    "scan_cache_file": "",
    "mesh_processed": [],
    "auto_save_paths": False,
    "texture_preset": "default_pbr",
    "material_overrides": {},
    "auto_resolve_fallback": True,
}

# Appearance keys written by older app versions. They now belong to
# application Settings and are stripped from profiles on load so a stale
# copy can't resurface into a save later.
_LEGACY_APPEARANCE_KEYS = ("color_scheme", "custom_schemes")


def _safe_path(name: str) -> Path:
    """Resolve ``profiles/<name>.json`` if and only if *name* is benign.

    Raises ``ValueError`` for empty/oversized names, names containing path
    separators or shell metacharacters, reserved Windows device names, and
    any name whose resolved file would escape ``_PROFILES_DIR`` (e.g.
    ``..\\evil`` or an absolute path).
    """
    if not isinstance(name, str):
        raise ValueError(f"Profile name must be a string, got {type(name).__name__}")

    ok, reason = ProfileManager.is_valid_name(name)
    if not ok:
        raise ValueError(f"Invalid profile name: {reason}")

    stripped = name.strip()
    if stripped.upper() in _RESERVED_NAMES:
        raise ValueError(f"Invalid profile name: '{stripped}' is a reserved Windows device name")

    candidate = (_PROFILES_DIR / f"{stripped}.json").resolve()
    parent = _PROFILES_DIR.resolve()
    try:
        candidate.relative_to(parent)
    except ValueError as e:
        raise ValueError(
            f"Profile name resolves outside the profiles directory: {stripped!r}"
        ) from e
    if candidate.parent != parent:
        raise ValueError(
            f"Profile name resolves outside the profiles directory: {stripped!r}"
        )
    return candidate


class ProfileManager:
    """Manages per-game profile JSON files in the ``profiles/`` directory."""

    def __init__(self) -> None:
        _PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def profiles_dir(self) -> Path:
        return _PROFILES_DIR

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[str]:
        """Return sorted list of profile names (stems of JSON files).

        Stems failing :meth:`is_valid_name` are filtered out — a malformed
        on-disk file should not crash the picker.
        """
        names: list[str] = []
        for p in _PROFILES_DIR.glob("*.json"):
            stem = p.stem
            ok, _ = self.is_valid_name(stem)
            if not ok:
                continue
            if stem.upper() in _RESERVED_NAMES:
                continue
            names.append(stem)
        return sorted(names)

    def load_profile(self, name: str) -> dict:
        """Load a profile by name. Returns a dict with default values filled in.

        Failure modes:

        - ``FileNotFoundError`` if the profile JSON does not exist.
        - ``ProfileLoadError`` if the file exists but the OS refuses to read
          it (locked, permission denied). The caller should surface this.
        - **Corrupt JSON is recovered silently:** the bad file is renamed to
          ``<name>.json.corrupt-<unix_ts>`` so the user can inspect/recover
          it manually, and an empty-defaults profile is returned. This keeps
          the GUI usable when a single profile gets mangled.
        """
        path = _safe_path(name)
        if not path.is_file():
            raise FileNotFoundError(f"Profile not found: {name}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except PermissionError as e:
            raise ProfileLoadError(
                f"Permission denied reading profile '{name}': {e}"
            ) from e
        except OSError as e:
            raise ProfileLoadError(
                f"OS error reading profile '{name}': {e}"
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            backup = self._quarantine_corrupt(path, raw, reason=f"JSONDecodeError: {e}")
            log.error(
                "Profile '%s' has invalid JSON (%s); quarantined to %s, "
                "returning empty defaults.", name, e, backup.name,
            )
            data = {}

        # Top-level must be an object; anything else (list, scalar, null) is
        # corrupt by our schema. Quarantine and recover the same way.
        if not isinstance(data, dict):
            backup = self._quarantine_corrupt(
                path, raw, reason=f"top-level was {type(data).__name__}, expected object"
            )
            log.error(
                "Profile '%s' has wrong shape (got %s, expected object); "
                "quarantined to %s, returning empty defaults.",
                name, type(data).__name__, backup.name,
            )
            data = {}

        # Fill missing keys from defaults; type-check collection keys.
        defaulted: list[str] = []
        for k, v in _EMPTY_PROFILE.items():
            if k not in data:
                data[k] = v
                defaulted.append(k)
                continue
            # Reject obviously corrupt/malicious values for collection keys.
            if k == "aes_keys" and not isinstance(data[k], list):
                log.warning("profile %s: aes_keys is %s, resetting to []", name, type(data[k]).__name__)
                data[k] = []
            elif k == "mesh_processed" and not isinstance(data[k], list):
                data[k] = []
            elif k == "auto_save_paths" and not isinstance(data[k], bool):
                data[k] = bool(data[k])
            elif k == "material_overrides" and not isinstance(data[k], dict):
                log.warning("profile %s: material_overrides is %s, resetting to {}", name, type(data[k]).__name__)
                data[k] = {}
            elif k == "auto_resolve_fallback" and not isinstance(data[k], bool):
                data[k] = bool(data[k])
            elif k == "texture_preset" and not isinstance(data[k], str):
                data[k] = "default_pbr"
        # Legacy 'psk_processed' → 'mesh_processed' migration. The Mesh Picker
        # renamed its processed-record key when it started handling PSK, GLB
        # and FBX files together; older profiles still carry the old name.
        # Merge (path-union) so neither a re-processed path nor a re-exported
        # mesh loses its record.
        legacy_processed = data.pop("psk_processed", [])
        if legacy_processed and isinstance(legacy_processed, list):
            merged = list(data.get("mesh_processed", []))
            seen = set(merged)
            for p in legacy_processed:
                if p and p not in seen:
                    merged.append(p)
                    seen.add(p)
            data["mesh_processed"] = merged

        # Drop legacy appearance keys — theme/scheme state is application
        # Settings (QSettings) and must not ride along in profile JSON.
        for legacy in _LEGACY_APPEARANCE_KEYS:
            if legacy in data:
                data.pop(legacy)
                log.debug(
                    "profile %s: dropped legacy appearance key %r",
                    name, legacy,
                )

        if defaulted:
            log.info("profile %s: filled defaults for %s", name, ", ".join(defaulted))

        data["name"] = name.strip()
        return data

    @staticmethod
    def _quarantine_corrupt(path: Path, raw: str, *, reason: str) -> Path:
        """Move a corrupt profile aside so the GUI can keep running.

        Writes the original bytes to ``<stem>.json.corrupt-<unix_ts>`` next to
        the original. If the rename fails (e.g. parent directory disappeared),
        falls back to writing a sibling file with the same suffix.
        """
        ts = int(time.time())
        backup = path.with_name(f"{path.stem}.json.corrupt-{ts}")
        try:
            path.replace(backup)
        except OSError:
            # If we can't rename, at least save the raw bytes nearby so the
            # user has *something* to recover from.
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError:
                log.exception(
                    "Failed to quarantine corrupt profile %s (%s)", path, reason,
                )
        return backup

    def save_profile(self, name: str, data: dict) -> None:
        """Write *data* to ``profiles/<name>.json``."""
        path = _safe_path(name)
        data["name"] = name.strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("Saved profile: %s", path)

    def delete_profile(self, name: str) -> None:
        path = _safe_path(name)
        if path.is_file():
            path.unlink()
            log.info("Deleted profile: %s", name)

    def rename_profile(self, old: str, new: str) -> None:
        """Rename a profile on disk.  Updates the ``name`` field inside."""
        old_path = _safe_path(old)
        new_path = _safe_path(new)
        if not old_path.is_file():
            raise FileNotFoundError(f"Profile not found: {old}")
        if new_path.exists():
            raise FileExistsError(f"Profile already exists: {new}")
        data = self.load_profile(old)
        data["name"] = new.strip()
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        old_path.unlink()
        log.info("Renamed profile: %s → %s", old, new)

    def profile_exists(self, name: str) -> bool:
        try:
            return _safe_path(name).is_file()
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # New profile helper
    # ------------------------------------------------------------------

    def create_profile(self, name: str, data: dict | None = None) -> dict:
        """Create a new profile with defaults, optionally merging *data*."""
        profile = dict(_EMPTY_PROFILE)
        if data:
            profile.update(data)
        profile["name"] = name.strip()
        self.save_profile(name, profile)
        return profile

    # ------------------------------------------------------------------
    # Migration from legacy QSettings
    # ------------------------------------------------------------------

    def migrate_from_qsettings(self, cfg_module) -> str | None:
        """If no profiles exist, create a 'Default' profile from existing QSettings.

        Returns the created profile name, or None if migration was skipped.
        """
        if self.list_profiles():
            return None  # profiles already exist

        import hashlib

        game_dir = cfg_module.get("game_folder") or ""
        ue_version = cfg_module.get("unpack_ue_version") or "GAME_UE5_4"
        unpack_out = cfg_module.get("unpack_output_dir") or ""
        blender_out = cfg_module.get("output_dir") or ""
        mappings = ""

        aes_keys: list[dict] = []
        raw_keys = cfg_module.get("aes_keys") or "[]"
        try:
            aes_keys = json.loads(raw_keys)
        except json.JSONDecodeError:
            pass

        # Determine scan cache filename for this game_dir
        scan_cache_file = ""
        if game_dir:
            folder_hash = hashlib.md5(game_dir.encode()).hexdigest()[:12]
            scan_cache_file = f"scan_{folder_hash}.json"

        profile = self.create_profile("Default", {
            "game_dir": game_dir,
            "ue_version": ue_version,
            "aes_keys": aes_keys,
            "unpack_output_dir": unpack_out,
            "blender_output_dir": blender_out,
            "mappings_path": mappings,
            "scan_cache_file": scan_cache_file,
            "mesh_processed": [],
        })
        log.info("Migrated QSettings → Default profile (game_dir=%s)", game_dir)
        return "Default"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_name(name: str) -> tuple[bool, str]:
        """Check if *name* is a valid profile name.  Returns (ok, reason)."""
        if not isinstance(name, str):
            return False, "Name must be a string."
        name = name.strip()
        if not name:
            return False, "Name cannot be empty."
        if _INVALID_CHARS.search(name):
            return False, 'Name cannot contain: \\ / : * ? " < > |'
        if name in (".", ".."):
            return False, "Invalid name."
        if len(name) > 100:
            return False, "Name too long (max 100 characters)."
        # Block control characters (NUL, newlines, tab, …) which would let
        # the resolved path slip through filesystem APIs in unexpected ways.
        if any(ord(c) < 0x20 for c in name):
            return False, "Name cannot contain control characters."
        if name.upper() in _RESERVED_NAMES:
            return False, f"'{name}' is a reserved Windows device name."
        return True, ""
