"""Pre-flight environment checks: required deps fail loudly, optionals warn.

Required failures here mean the app itself won't run. Optional warnings
surface what binary/runtime smoke tests will be skipped on this machine.
"""

from __future__ import annotations

import ctypes
import importlib
import importlib.util
import os
import shutil
import sys
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Required dependencies — these block the app from running
# ---------------------------------------------------------------------------

def test_python_version_is_3_11_or_newer():
    assert sys.version_info >= (3, 11), (
        f"Python 3.11+ required, found {sys.version_info[:3]}"
    )


def test_pyside6_importable_with_min_version():
    PySide6 = importlib.import_module("PySide6")
    version = getattr(PySide6, "__version__", "0.0.0")
    parts = tuple(int(p) for p in version.split(".")[:2])
    assert parts >= (6, 6), f"PySide6 >= 6.6 required, found {version}"


def test_pillow_importable():
    Image = importlib.import_module("PIL.Image")
    assert hasattr(Image, "open")


def test_pytest_qt_available():
    """The Qt tier needs pytest-qt — fail if a contributor forgets it."""
    spec = importlib.util.find_spec("pytestqt")
    assert spec is not None, (
        "pytest-qt not installed — run: py -m pip install -r requirements-dev.txt"
    )


def test_repo_data_files_exist():
    presets = REPO_ROOT / "data" / "texture_presets.json"
    assert presets.is_file(), f"Missing source-of-truth file: {presets}"


def test_base_dir_resolves():
    """`_base.base_dir()` must resolve to the repo root in source mode."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from _base import base_dir
        resolved = base_dir()
        assert resolved.is_dir(), f"base_dir() returned non-existent: {resolved}"
        assert (resolved / "main.py").is_file(), (
            f"base_dir() {resolved} does not contain main.py"
        )
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Optional binaries / runtimes — warn but never fail
# ---------------------------------------------------------------------------

def test_optional_pyinstaller_available_for_build():
    """PyInstaller is needed for build.bat but not for running tests/app."""
    spec = importlib.util.find_spec("PyInstaller")
    if spec is None:
        warnings.warn(
            "PyInstaller not installed — build.bat will install it on demand",
            UserWarning,
            stacklevel=2,
        )


def test_optional_platform_is_windows():
    if sys.platform not in ("win32", "linux", "darwin"):
        warnings.warn(
            f"Running on an unexpected platform {sys.platform!r}; the app is "
            "tested on Windows and Linux. Tests still run.",
            UserWarning,
            stacklevel=2,
        )


def test_optional_blender_detected():
    candidate = os.environ.get("BLENDER_EXE")
    if candidate and Path(candidate).is_file():
        return
    if shutil.which("blender") is not None:
        return
    warnings.warn(
        "Blender executable not found (set BLENDER_EXE env var or add to PATH). "
        "Blender-dependent e2e tests will be skipped.",
        UserWarning,
        stacklevel=2,
    )


def test_optional_dotnet_runtime_detected():
    if shutil.which("dotnet") is None:
        warnings.warn(
            ".NET SDK/runtime not found on PATH. CUE4ParseCLI cannot be (re)built "
            "and the dotnet_cli e2e smoke will skip.",
            UserWarning,
            stacklevel=2,
        )


def test_optional_everything_dll_loadable():
    if sys.platform != "win32":
        return  # silently skip on non-Windows; covered by platform warning
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
            return
        except OSError:
            continue
    warnings.warn(
        "Everything64.dll not loadable from any default location. "
        "The app needs Everything desktop installed. Live SDK tests will skip.",
        UserWarning,
        stacklevel=2,
    )


def test_optional_cue4parse_cli_built():
    candidate = os.environ.get("CUE4PARSE_CLI")
    if candidate and Path(candidate).is_file():
        return
    name = "CUE4ParseCLI.exe" if sys.platform == "win32" else "CUE4ParseCLI"
    default = REPO_ROOT / "cue4parse_cli" / "bin" / "publish" / name
    if default.is_file():
        return
    warnings.warn(
        f"CUE4ParseCLI not built ({default} missing). Run build_cli.bat or "
        "build_cli.sh to produce it. Unpacker functionality will be unavailable.",
        UserWarning,
        stacklevel=2,
    )
