"""NixOS platform detection and nix-profile path discovery.

NixOS differs from other Linux distros in two ways that matter here:

- There is no FHS filesystem: ``/usr/bin``, ``/usr/local/bin``, ``/opt`` and
  ``/snap`` do not exist. Nix-installed tools (Blender, ffmpeg) live under
  ``/nix/store/...`` and are exposed on PATH (and, for system/dev profiles,
  in the well-known profile bin dirs listed by :func:`nixos_bin_dirs`).
- Published Linux binaries (the self-contained CUE4ParseCLI) expect the FHS
  dynamic loader ``/lib64/ld-linux-x86-64.so.2``, which only exists when the
  user enables ``programs.nix-ld``. The official wiki documents nix-ld —
  it recreates ``/lib/ld-linux*`` and honors ``NIX_LD``/``NIX_LD_LIBRARY_PATH``
  — as the standard way to run such binaries (https://wiki.nixos.org/wiki/Nix-ld).

Detection reads one small file and caches it for the process lifetime; there
are no subprocesses, threads or timers here, so nothing in this module can
leak. Tests reset the cache via :func:`reset_cache`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Default path by the nix-ld module (it symlinks the GNU loader into /lib64
# so unpatched ELF binaries start with no per-app work).
_LD_LINUX_X86_64 = "/lib64/ld-linux-x86-64.so.2"


def _read_os_release() -> dict:
    """Parse /etc/os-release (id=... fields) into a dict; {} when missing."""
    data: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.strip().partition("=")
                    data[key] = value.strip('"')
    except OSError:
        pass
    return data


_is_nixos: bool | None = None
_has_nix_ld: bool | None = None
_steam_run: str | None = None


def is_nixos() -> bool:
    """True when the host is NixOS (``ID=nixos`` in /etc/os-release)."""
    global _is_nixos
    if _is_nixos is None:
        release = _read_os_release()
        # ID_LIKE can also carry nixos on derivatives; check both.
        _is_nixos = (
            release.get("ID") == "nixos"
            or any(v == "nixos" for v in release.get("ID_LIKE", "").split(" "))
        )
    return _is_nixos


def has_nix_ld() -> bool:
    """True when the FHS ELF loader (nix-ld) or NIX_LD is available."""
    global _has_nix_ld
    if _has_nix_ld is None:
        _has_nix_ld = bool(os.environ.get("NIX_LD")) or os.path.exists(_LD_LINUX_X86_64)
    return _has_nix_ld


def nixos_bin_dirs() -> list[Path]:
    """Well-known bin dirs exposing NixOS profile-installed tools.

    Covers system packages (current-system), user packages (per-user profile
    and /etc/profiles), dev/global nix profiles, and security wrappers. Only
    dirs that exist are returned.
    """
    candidates = [
        Path("/run/current-system/sw/bin"),
        Path("/run/wrappers/bin"),
        Path("/etc/profiles/per-user") / os.environ.get("USER", "") / "bin",
        Path.home() / ".nix-profile/bin",
        Path("/nix/var/nix/profiles/default/bin"),
    ]
    return [p for p in candidates if p.is_dir()]


def find_tool_in_nix_dirs(name: str) -> str:
    """First existing ``<name>`` under the NixOS profile bin dirs, or ""."""
    for d in nixos_bin_dirs():
        candidate = d / name
        if candidate.is_file():
            return str(candidate)
    return ""


def steam_run_path() -> str:
    """Path to steam-run, or "".

    Cached once: a miss stays a miss for the session (Steam must actually be
    installed for steam-run to exist; re-probing per CLI launch only burns
    cycles and risks differing answers mid-session).
    """
    global _steam_run
    if _steam_run is None:
        _steam_run = shutil.which("steam-run") or shutil.which("steam-run-original") or ""
    return _steam_run


def reset_cache() -> None:
    """Drop all cached probes (tests only)."""
    global _is_nixos, _has_nix_ld, _steam_run
    _is_nixos = None
    _has_nix_ld = None
    _steam_run = None
