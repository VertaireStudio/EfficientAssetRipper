"""Platform-aware detection of external tool paths.

Single source of truth for finding the bundled/published CUE4ParseCLI and
Blender across Windows/Linux/macOS. GUI panels and the setup wizard both call
into here so candidate lists cannot drift apart.
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile
from pathlib import Path

from _base import base_dir

_log = None


def _logger():
    global _log
    if _log is None:
        import logging
        _log = logging.getLogger(__name__)
    return _log


def cue4parse_cli_name() -> str:
    """Return the published CLI binary name for the current platform."""
    return "CUE4ParseCLI.exe" if sys.platform == "win32" else "CUE4ParseCLI"


def bundled_cue4parse_cli() -> Path:
    """Return the in-tree published CLI path (may not exist yet)."""
    return base_dir() / "cue4parse_cli" / "bin" / "publish" / cue4parse_cli_name()


def _steam_library_roots() -> list[Path]:
    """Steam library roots: the default user dir plus any listed in libraryfolders.vdf."""
    roots: list[Path] = []

    default_root = Path.home() / ".local/share/Steam"
    if default_root.is_dir():
        roots.append(default_root)

    # ~/.steam/steam is a symlink Steam maintains; its steamapps/libraryfolders.vdf
    # lists every library root ("path" entries), including secondary disks.
    vdf = Path.home() / ".steam/steam/steamapps/libraryfolders.vdf"
    try:
        if vdf.is_file():
            for line in vdf.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith('"path"'):
                    raw = line.split('"', 2)[2]
                    # Unescape VDF: '\\\\' → '\\'
                    raw = raw.replace("\\\\", "\\")
                    if raw:
                        roots.append(Path(raw))
    except OSError:
        pass

    return roots


def detect_blender() -> str:
    """Return the first existing Blender executable path, or "".

    Windows: standard per-version install dirs. Linux/macOS: PATH first,
    then distro/flatpak/snap/Steam install locations. Steam's Linux build of
    Blender keeps the binary at <steamapp>/blender (no version subfolder).
    """
    if sys.platform == "win32":
        candidates = [
            rf"C:\Program Files\Blender Foundation\Blender {v}\blender.exe"
            for v in ("4.4", "4.3", "4.2", "4.1", "4.0")
        ]
    else:
        candidates = [shutil.which("blender") or ""]

        # NixOS: shutil.which covers a shell session, but the GUI can start
        # without the user's nix profile dirs on PATH (e.g. from a .desktop
        # entry); probe the well-known profile bin dirs too.
        if sys.platform == "linux":
            from core.platform_detect import is_nixos, find_tool_in_nix_dirs
            if is_nixos():
                candidates.append(find_tool_in_nix_dirs("blender"))

        candidates += [
            "/usr/bin/blender",
            "/usr/local/bin/blender",
            "/opt/blender/blender",
            "/snap/bin/blender",
            "/snap/blender/current/blender",
            str(Path.home() / ".local/share/Steam/steamapps/common/Blender/blender"),
            str(Path.home() / ".steam/steam/steamapps/common/Blender/blender"),
        ]

        # Flatpak (per-user and system installs).
        for flatpak_root in (
            Path.home() / ".local/share/flatpak/app/org.blender.Blender",
            Path("/var/lib/flatpak/app/org.blender.Blender"),
        ):
            if flatpak_root.is_dir():
                candidates += glob.glob(
                    str(flatpak_root / "*" / "active" / "files" / "bin" / "blender")
                )

        # Every Steam library root (default one plus any secondary library).
        candidates += [
            str(root / "steamapps/common/Blender/blender")
            for root in _steam_library_roots()
        ]

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def detect_everything_dll() -> str:
    """Return the first existing Everything SDK DLL path, or "".

    Everything is Windows-only software; returns "" on other platforms and
    callers should treat the built-in WalkSearcher fallback as the expected
    (working) outcome there.
    """
    if sys.platform != "win32":
        return ""
    candidates = [
        r"C:\Program Files\Everything\Everything64.dll",
        r"C:\Program Files (x86)\Everything\Everything64.dll",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def detect_ffmpeg() -> str:
    """Return a usable ffmpeg binary path, or "".

    Resolution mirrors the CUE4Parse CLI (cue4parse_cli/Program.cs
    InitFfmpeg): its hash-verified download cache first, then a system
    ffmpeg on PATH (plus common Windows install locations). An empty
    result means the CLI auto-downloads its pinned build on the first
    MP3/WebM audio export.
    """
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    cache = Path(tempfile.gettempdir()) / "CUE4ParseCLI_ffmpeg"
    for bin_dir in cache.glob("*/bin"):
        candidate = bin_dir / exe_name
        if candidate.is_file():
            return str(candidate)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if sys.platform == "win32":
        for c in (
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
        ):
            if os.path.isfile(c):
                return c
    elif sys.platform == "linux":
        from core.platform_detect import is_nixos, find_tool_in_nix_dirs
        if is_nixos():
            nix = find_tool_in_nix_dirs("ffmpeg")
            if nix:
                return nix
    return ""


def detect_cue4parse_cli() -> str:
    """Return a usable CUE4ParseCLI path, or "" if none is published.

    Resolution: user-configured path (checked by callers, e.g.
    ``config.get_cue4parse_cli``), then the in-tree published binary.
    """
    p = bundled_cue4parse_cli()
    if p.is_file():
        return str(p)
    return ""
