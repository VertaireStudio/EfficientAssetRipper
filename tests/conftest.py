"""Shared pytest fixtures for the EfficientAssetRipper test suite.

Fixtures here are auto-discovered for every test under `tests/`. Tier-specific
fixtures (qtbot extensions, etc.) live in nested conftest.py files.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Repo / fixture locations
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the project root (where main.py lives)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures"


@pytest.fixture(scope="session")
def real_cache_dir(repo_root: Path) -> Path:
    return repo_root / "cache"


@pytest.fixture(scope="session")
def real_data_dir(repo_root: Path) -> Path:
    return repo_root / "data"


# ---------------------------------------------------------------------------
# Real-data fixtures (skip cleanly when data is absent on a fresh clone)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def real_presets(real_data_dir: Path) -> dict:
    presets_path = real_data_dir / "texture_presets.json"
    if not presets_path.is_file():
        pytest.skip(f"texture_presets.json not present at {presets_path}")
    return json.loads(presets_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def tiny_presets() -> dict:
    """Minimal in-memory presets for deterministic tests."""
    return {
        "version": 1,
        "ignore_textures": ["DefaultTexture"],
        "ignore_patterns": ["BlendFunc"],
        "presets": {
            "default_pbr": {
                "description": "Test preset",
                "priority_order": ["base_color", "normal", "orm"],
                "texture_slots": {
                    "base_color": {
                        "suffixes": ["_C", "_BaseColor"],
                        "param_names": ["BaseColor", "Diffuse"],
                        "colorspace": "sRGB",
                        "wiring": {"type": "direct", "target_input": "Base Color"},
                    },
                    "normal": {
                        "suffixes": ["_N", "_Normal"],
                        "param_names": ["Normal"],
                        "colorspace": "Non-Color",
                        "wiring": {"type": "normal_map", "target_input": "Normal"},
                    },
                    "orm": {
                        "suffixes": ["_ORM"],
                        "param_names": [],
                        "colorspace": "Non-Color",
                        "wiring": {"type": "split_channels"},
                    },
                },
            }
        },
        "material_overrides": {},
    }


@pytest.fixture(scope="session")
def jedi_scan_dict(real_cache_dir: Path) -> dict:
    """Parse the 2.5 MB Jedi Survivor scan once per session (skip if absent)."""
    path = real_cache_dir / "scan_b6df0cbbd18d.json"
    if not path.is_file():
        pytest.skip("Jedi Survivor scan cache fixture not present")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def small_scan_dict(real_cache_dir: Path) -> dict:
    """Parse the small scan cache once per session (skip if absent)."""
    path = real_cache_dir / "scan_205cef9323eb.json"
    if not path.is_file():
        pytest.skip("Small scan cache fixture not present")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# QSettings stub (avoids polluting the user's real Windows registry)
# ---------------------------------------------------------------------------

class _MockQSettings:
    """Minimal stand-in for PySide6.QtCore.QSettings backed by a dict."""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def value(self, key: str, default: object = "") -> object:
        return self._data.get(key, default)

    def setValue(self, key: str, value: object) -> None:
        self._data[key] = value

    def contains(self, key: str) -> bool:
        return key in self._data

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


@pytest.fixture
def mock_qsettings(monkeypatch) -> _MockQSettings:
    """Replace `config._qs` with an in-memory dict for this test."""
    import config

    stub = _MockQSettings()
    monkeypatch.setattr(config, "_qs", stub)
    return stub


# ---------------------------------------------------------------------------
# Fake Everything SDK — no DLL, no IPC
# ---------------------------------------------------------------------------

class FakeEverythingSDK:
    """Stand-in for `core.everything.EverythingSDK` for tests.

    Seed it with explicit lookups; methods return whatever you put in. No
    network/file I/O, no ctypes, safe to use in unit tests.
    """

    def __init__(
        self,
        textures: Optional[dict[str, list[Path]]] = None,
        psk_files: Optional[list[Path]] = None,
        props_files: Optional[dict[str, list[Path]]] = None,
        folder_textures: Optional[dict[str, list[Path]]] = None,
    ) -> None:
        # Lookups are case-insensitive on the key
        self._textures: dict[str, list[Path]] = {
            k.lower(): list(v) for k, v in (textures or {}).items()
        }
        self._psk_files: list[Path] = list(psk_files or [])
        self._props_files: dict[str, list[Path]] = {
            k.lower(): list(v) for k, v in (props_files or {}).items()
        }
        # folder_textures: map normalized folder path → list of TGA paths
        # under it. Used by find_textures_in_folder for keyword fallback tests.
        self._folder_textures: dict[str, list[Path]] = {
            os.path.normcase(str(k)): list(v)
            for k, v in (folder_textures or {}).items()
        }
        self.calls: list[tuple[str, tuple, dict]] = []

    # --- recording helpers -------------------------------------------------

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    # --- API methods used by the production code ---------------------------

    def find_texture(self, texture_name: str, folder: str = "") -> list[Path]:
        self._record("find_texture", texture_name, folder=folder)
        return list(self._textures.get(texture_name.lower(), []))

    def find_textures_in_folder(
        self, folder: str, ext: str = "tga", max_results: int = 5_000
    ) -> list[Path]:
        self._record(
            "find_textures_in_folder", folder, ext=ext, max_results=max_results
        )
        if not folder:
            return []
        # Match seeded folder mappings recursive-substring style: any seeded
        # key whose path is the same as or beneath *folder* contributes.
        folder_norm = os.path.normcase(str(folder)).rstrip("\\/")
        out: list[Path] = []
        for key, paths in self._folder_textures.items():
            if key == folder_norm or key.startswith(folder_norm + os.sep):
                out.extend(paths)
                if len(out) >= max_results:
                    return out[:max_results]
        return out

    def find_psk_files(self, folder: str = "") -> list[Path]:
        self._record("find_psk_files", folder=folder)
        return list(self._psk_files)

    def find_mesh_files(self, folder: str = "") -> list[Path]:
        """Same seeded list as find_psk_files — the fake is format-agnostic."""
        self._record("find_mesh_files", folder=folder)
        return list(self._psk_files)

    def find_props_file(self, name: str, folder: str = "") -> list[Path]:
        self._record("find_props_file", name, folder=folder)
        return list(self._props_files.get(name.lower(), []))

    def search(self, query: str, max_results: int = 100, sort: int = 0) -> list[str]:
        self._record("search", query, max_results=max_results, sort=sort)
        return []

    def search_file(
        self,
        filename: str,
        extension: str = "",
        folder: str = "",
        max_results: int = 50,
    ) -> list[Path]:
        self._record("search_file", filename, extension=extension, folder=folder)
        return list(self._textures.get(filename.lower(), []))

    def test_connection(self) -> tuple[bool, str]:
        return True, "ok (FakeEverythingSDK)"


@pytest.fixture
def fake_sdk() -> FakeEverythingSDK:
    """Empty FakeEverythingSDK; tests populate it via the .seed_* helpers below."""
    return FakeEverythingSDK()


@pytest.fixture
def make_fake_sdk():
    """Factory: FakeEverythingSDK seeded with caller-supplied dicts."""

    def _factory(
        textures: Optional[dict[str, list[Path]]] = None,
        psk_files: Optional[list[Path]] = None,
        props_files: Optional[dict[str, list[Path]]] = None,
        folder_textures: Optional[dict[str, list[Path]]] = None,
    ) -> FakeEverythingSDK:
        return FakeEverythingSDK(
            textures=textures,
            psk_files=psk_files,
            props_files=props_files,
            folder_textures=folder_textures,
        )

    return _factory


# ---------------------------------------------------------------------------
# Profile manager — redirect to tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_profiles_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ProfileManager._PROFILES_DIR to a fresh tmp directory."""
    import core.profile_manager as pm

    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pm, "_PROFILES_DIR", profiles)
    return profiles


# ---------------------------------------------------------------------------
# Blender runner — patched return
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_blender_run(monkeypatch):
    """Patch `core.blender_runner.run_blender` to return a canned result.

    Yields a dict with:
      - ``calls``: list of recorded call kwargs
      - ``responses``: append :class:`BlenderResult` instances to control
        consecutive return values; the default is a generic success.
      - ``write_dummy_blend_on_success``: when True (default False), write a
        tiny placeholder file at ``manifest['output_path']`` whenever the
        canned result reports success. Used by the synthetic e2e pipeline
        test so downstream "blend exists?" checks pass without spawning
        Blender.
    """
    from core.blender_runner import BlenderResult

    calls: list[dict] = []
    responses: list[BlenderResult] = []
    state: dict = {"write_dummy_blend_on_success": False}

    def _stub(
        blender_exe,
        manifest,
        timeout=120,
        on_status=None,
        cancel_check=None,
        on_proc_started=None,
    ):
        calls.append(
            {
                "blender_exe": blender_exe,
                "manifest": manifest,
                "timeout": timeout,
                "on_status": on_status,
                "cancel_check": cancel_check,
                "on_proc_started": on_proc_started,
            }
        )
        if responses:
            result = responses.pop(0)
        else:
            result = BlenderResult(
                success=True,
                asset_name=manifest.get("psk_path", ""),
                materials_processed=len(manifest.get("materials", {})),
                return_code=0,
                stdout="",
                stderr="",
            )

        if result.success and state["write_dummy_blend_on_success"]:
            out = manifest.get("output_path")
            if out:
                out_path = Path(out)
                try:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    # Real .blend files start with "BLENDER" — using the
                    # same magic keeps anything that sniffs the header happy
                    # without needing the full container format.
                    out_path.write_bytes(b"BLENDER-test-stub\n")
                except OSError:
                    pass

        return result

    import core.blender_runner as br
    monkeypatch.setattr(br, "run_blender", _stub)
    # Also patch the import inside core.job_manager (it imported the symbol)
    import core.job_manager as jm
    monkeypatch.setattr(jm, "run_blender", _stub)

    return {
        "calls": calls,
        "responses": responses,
        "write_dummy_blend_on_success": state,
    }


# ---------------------------------------------------------------------------
# Skip auto-detection for `requires_*` markers
# ---------------------------------------------------------------------------

def _has_blender() -> bool:
    candidate = os.environ.get("BLENDER_EXE")
    if candidate and Path(candidate).is_file():
        return True
    if shutil.which("blender") is not None:
        return True
    # Platform-aware fallback (flatpak, snap, Steam libraries, …)
    try:
        from core.tools_detect import detect_blender
    except Exception:
        return False
    return detect_blender() != ""


def _has_dotnet_cli() -> bool:
    candidate = os.environ.get("CUE4PARSE_CLI")
    if candidate and Path(candidate).is_file():
        return True
    # Default in-tree publish path (platform-dependent binary name)
    try:
        from core.tools_detect import bundled_cue4parse_cli
    except Exception:
        name = "CUE4ParseCLI.exe" if sys.platform == "win32" else "CUE4ParseCLI"
        default = (
            Path(__file__).resolve().parent.parent
            / "cue4parse_cli" / "bin" / "publish" / name
        )
        return default.is_file()
    return bundled_cue4parse_cli().is_file()


def _has_everything_dll() -> bool:
    if sys.platform != "win32":
        return False
    candidate = os.environ.get("EVERYTHING_DLL")
    paths = [candidate] if candidate else [
        r"C:\Program Files\Everything\Everything64.dll",
        r"C:\Program Files (x86)\Everything\Everything64.dll",
        "Everything64.dll",
    ]
    for p in paths:
        if not p:
            continue
        try:
            ctypes.WinDLL(p)
            return True
        except OSError:
            continue
    return False


def pytest_collection_modifyitems(config, items):
    skip_blender = pytest.mark.skip(reason="blender executable not detected")
    skip_dotnet = pytest.mark.skip(reason="CUE4ParseCLI.exe not detected")
    skip_everything = pytest.mark.skip(reason="Everything64.dll not loadable")
    skip_nonwindows = pytest.mark.skip(reason="windows-only test")

    have_blender = _has_blender()
    have_dotnet = _has_dotnet_cli()
    have_everything = _has_everything_dll()
    is_windows = sys.platform == "win32"

    for item in items:
        if "requires_blender" in item.keywords and not have_blender:
            item.add_marker(skip_blender)
        if "requires_dotnet_cli" in item.keywords and not have_dotnet:
            item.add_marker(skip_dotnet)
        if "requires_everything" in item.keywords and not have_everything:
            item.add_marker(skip_everything)
        if "windows_only" in item.keywords and not is_windows:
            item.add_marker(skip_nonwindows)
