"""End-to-end smoke tests for the built CUE4ParseCLI.exe.

Skipped automatically when the exe is absent. Set CUE4PARSE_CLI to override
the default in-tree publish location.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.requires_dotnet_cli, pytest.mark.slow]


def _resolve_cli() -> str:
    cli = os.environ.get("CUE4PARSE_CLI")
    if cli and Path(cli).is_file():
        return cli
    repo_root = Path(__file__).resolve().parent.parent.parent
    name = "CUE4ParseCLI.exe" if sys.platform == "win32" else "CUE4ParseCLI"
    default = repo_root / "cue4parse_cli" / "bin" / "publish" / name
    return str(default)


def test_cli_starts_and_quits_cleanly():
    """Start the CLI, send `quit`, expect exit 0 within 5 seconds."""
    cli = _resolve_cli()
    proc = subprocess.Popen(
        [cli],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,  # NDJSON is bytes
    )
    try:
        proc.stdin.write(json.dumps({"cmd": "quit"}).encode("utf-8") + b"\n")
        proc.stdin.flush()
        rc = proc.wait(timeout=10)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_cli_help_flag_prints_and_exits():
    """--help returns exit 0 and prints the NDJSON banner (Phase 3.1)."""
    cli = _resolve_cli()
    out = subprocess.run(
        [cli, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0
    assert "NDJSON" in out.stdout
    assert "init" in out.stdout
    assert "quit" in out.stdout


def test_cli_h_short_flag_prints_and_exits():
    """-h is recognized as an alias for --help (Phase 3.1)."""
    cli = _resolve_cli()
    out = subprocess.run([cli, "-h"], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    assert "NDJSON" in out.stdout


def test_cli_stdout_is_utf8_lf(tmp_path):
    """Stdout NDJSON must be valid UTF-8 with LF (not CRLF) newlines (Phase 2.1)."""
    cli = _resolve_cli()
    proc = subprocess.Popen(
        [cli],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(json.dumps({"cmd": "quit"}).encode("utf-8") + b"\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    # Decode strictly: any non-UTF-8 bytes will raise.
    text = out.decode("utf-8")
    # Bare \n, never \r\n. A CRLF reply is the failure mode.
    assert b"\r\n" not in out
    # quit_ack should be present
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(msg.get("type") == "quit_ack" for msg in parsed)


def test_cli_oversize_stdin_line_emits_error(tmp_path):
    """A 5 MB unbounded line on stdin must produce a structured error (Phase 3.2)."""
    cli = _resolve_cli()
    proc = subprocess.Popen(
        [cli],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # ~5 MB of garbage with no newline, then a real quit.
        garbage = b"x" * (5 * 1024 * 1024)
        proc.stdin.write(garbage + b"\n")
        proc.stdin.write(json.dumps({"cmd": "quit"}).encode("utf-8") + b"\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    text = out.decode("utf-8", errors="replace")
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(
        msg.get("type") == "error" and "too large" in msg.get("message", "")
        for msg in parsed
    ), f"missing 'input line too large' error in: {parsed!r}"


def test_cli_path_traversal_blocked_no_file_written(tmp_path):
    """Export with `../../foo.uasset` must not write outside output_dir (Phase 1.1).

    The CLI may surface the rejection as either a `failed` entry on
    `export_done` (if SafeJoin races ahead of the cancel) or as nothing at all
    if the iteration cancelled first — but in no scenario may a file land
    outside outputDir.
    """
    cli = _resolve_cli()
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()

    # Compute a relative traversal that would land in sentinel_dir if SafeJoin
    # didn't kick in — `../sentinel/payload.uasset` from out_dir's perspective.
    cmds = [
        {"cmd": "init", "game_dir": str(game_dir), "ue_version": "GAME_UE5_4"},
        {"cmd": "export", "paths": ["../sentinel/payload.uasset"], "output_dir": str(out_dir)},
    ]
    payload = b"".join((json.dumps(c) + "\n").encode("utf-8") for c in cmds)

    proc = subprocess.Popen(
        [cli], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
        # Wait for export_done before sending quit — avoids the cancel race
        # so we can actually observe whether the failed list captured the
        # SafeJoin rejection.
        deadline = time.monotonic() + 30
        events = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            events.append(msg)
            if msg.get("type") == "export_done":
                break
        proc.stdin.write((json.dumps({"cmd": "quit"}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    # Sentinel must remain untouched regardless of how the CLI surfaced the
    # rejection — that's the actual security property.
    assert list(sentinel_dir.iterdir()) == [], (
        f"Path traversal escaped containment: {list(sentinel_dir.iterdir())!r}"
    )

    # Belt-and-braces: when an export_done lists the path under failed, the
    # error string should mention path containment.
    export_done = next((e for e in events if e.get("type") == "export_done"), None)
    assert export_done is not None
    failed_paths = [f.get("path") for f in export_done.get("failed", [])]
    if "../sentinel/payload.uasset" in failed_paths:
        err = next(f["error"] for f in export_done["failed"] if f["path"] == "../sentinel/payload.uasset")
        assert "escapes" in err.lower() or "root" in err.lower()


def test_cli_init_without_provider_then_browse_emits_error():
    """Sending `browse` without prior `init` must emit a structured error (lifecycle)."""
    cli = _resolve_cli()
    proc = subprocess.Popen(
        [cli],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(json.dumps({"cmd": "browse", "path": "/"}).encode("utf-8") + b"\n")
        proc.stdin.write(json.dumps({"cmd": "quit"}).encode("utf-8") + b"\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    text = out.decode("utf-8", errors="replace")
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(
        msg.get("type") == "error" and "Not initialized" in msg.get("message", "")
        for msg in parsed
    ), f"missing 'Not initialized' error in: {parsed!r}"
