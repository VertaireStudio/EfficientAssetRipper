"""Unit tests for NixOS platform support (core.platform_detect + friends).

Monkeypatches fake /etc/os-release paths, loaders and profile bins via
monkeypatch on the module's helpers — no real NixOS needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import platform_detect, tools_detect

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_platform_cache(monkeypatch):
    """Fresh detection state per test (module cache is process-wide)."""
    platform_detect.reset_cache()
    monkeypatch.setattr(platform_detect.os, "environ", {})
    yield
    platform_detect.reset_cache()


def _fake_os_release(monkeypatch, content: str):
    def fake_read():
        data = {}
        for line in content.splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.strip().partition("=")
                data[key] = value.strip('"')
        return data

    monkeypatch.setattr(platform_detect, "_read_os_release", fake_read)


def test_is_nixos_true(monkeypatch):
    _fake_os_release(monkeypatch, 'ID=nixos\nID_LIKE="acorn"\n')
    assert platform_detect.is_nixos() is True


def test_is_nixos_id_like_true(monkeypatch):
    _fake_os_release(monkeypatch, 'ID=foo\nID_LIKE="nixos linux"\n')
    assert platform_detect.is_nixos() is True


def test_is_nixos_false(monkeypatch):
    _fake_os_release(monkeypatch, 'ID=ubuntu\nID_LIKE="debian"\n')
    assert platform_detect.is_nixos() is False


def test_is_nixos_missing_os_release(monkeypatch):
    monkeypatch.setattr(platform_detect, "_read_os_release", dict)
    assert platform_detect.is_nixos() is False


def test_has_nix_ld_loader_present(monkeypatch):
    monkeypatch.setattr(platform_detect.os.path, "exists", lambda p: p == "/lib64/ld-linux-x86-64.so.2")
    assert platform_detect.has_nix_ld() is True


def test_has_nix_ld_env(monkeypatch):
    monkeypatch.setattr(platform_detect.os.path, "exists", lambda p: False)
    monkeypatch.setattr(platform_detect.os, "environ", {"NIX_LD": "/nix/store/x/ld-linux"})
    assert platform_detect.has_nix_ld() is True


def test_has_nix_ld_absent(monkeypatch):
    monkeypatch.setattr(platform_detect.os.path, "exists", lambda p: False)
    assert platform_detect.has_nix_ld() is False


def test_nixos_bin_dirs_only_existing(monkeypatch, tmp_path: Path):
    existing = tmp_path / "sw" / "bin"
    existing.mkdir(parents=True)
    monkeypatch.setattr(
        platform_detect, "nixos_bin_dirs", lambda: [existing, tmp_path / "missing"]
    )
    tool = existing / "blender"
    tool.touch()
    monkeypatch.setattr(platform_detect.os, "environ", {"USER": "u"})
    assert platform_detect.find_tool_in_nix_dirs("blender") == str(tool)
    assert platform_detect.find_tool_in_nix_dirs("ffmpeg") == ""


def test_steam_run_path_cached(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        # force probing the fallback name too
        return f"/fake/{name}" if name == "steam-run-original" else None

    monkeypatch.setattr(platform_detect.shutil, "which", fake_which)
    assert platform_detect.steam_run_path() == "/fake/steam-run-original"
    # Cached: a second call must not re-probe.
    assert platform_detect.steam_run_path() == "/fake/steam-run-original"
    assert calls == ["steam-run", "steam-run-original"]


def test_steam_run_path_not_found_cached(monkeypatch):
    monkeypatch.setattr(platform_detect.shutil, "which", lambda name: None)
    assert platform_detect.steam_run_path() == ""
    assert platform_detect.steam_run_path() == ""


# ---------------------------------------------------------------------------
# tools_detect integration (NixOS candidate injection)
# ---------------------------------------------------------------------------


def test_detect_blender_nixos_profile_dir(monkeypatch, tmp_path: Path):
    if tools_detect.sys.platform != "linux":
        pytest.skip("linux-only candidate ordering")
    _fake_os_release(monkeypatch, "ID=nixos\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    blender = bin_dir / "blender"
    blender.touch()
    monkeypatch.setattr(
        platform_detect, "find_tool_in_nix_dirs",
        lambda name: str(blender) if name == "blender" else "",
    )
    monkeypatch.setattr(tools_detect.shutil, "which", lambda name: None)
    # Only the nix-dir result exists; every FHS candidate path is missing.
    monkeypatch.setattr(tools_detect.os.path, "isfile", lambda p: p == str(blender))

    assert tools_detect.detect_blender() == str(blender)


def test_detect_ffmpeg_nixos_profile_dir(monkeypatch, tmp_path: Path):
    if tools_detect.sys.platform != "linux":
        pytest.skip("linux-only candidate ordering")
    _fake_os_release(monkeypatch, "ID=nixos\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.touch()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(tools_detect.tempfile, "gettempdir", lambda: str(empty))
    monkeypatch.setattr(tools_detect.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        platform_detect, "find_tool_in_nix_dirs",
        lambda name: str(ffmpeg) if name == "ffmpeg" else "",
    )
    assert tools_detect.detect_ffmpeg() == str(ffmpeg)


def test_detect_ffmpeg_non_nixos_skips_nix_dirs(monkeypatch, tmp_path: Path):
    if tools_detect.sys.platform != "linux":
        pytest.skip("linux-only candidate ordering")
    _fake_os_release(monkeypatch, "ID=ubuntu\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(tools_detect.tempfile, "gettempdir", lambda: str(empty))
    monkeypatch.setattr(tools_detect.shutil, "which", lambda name: None)
    probe_calls: list[str] = []

    def record_probe(name: str) -> str:
        probe_calls.append(name)
        return f"/fake/{name}"

    monkeypatch.setattr(platform_detect, "find_tool_in_nix_dirs", record_probe)
    assert tools_detect.detect_ffmpeg() == ""
    # Off-NixOS: the nix-dir probe must never run.
    assert probe_calls == []
