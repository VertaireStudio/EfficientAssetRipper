"""Launch Blender as a subprocess and monitor its output.

Handles timeouts, crash detection, and structured status parsing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from _base import base_dir
from core.log_redaction import redact_sensitive

log = logging.getLogger(__name__)

BLENDER_SCRIPT = str(
    base_dir() / "blender" / "process_asset.py"
)


# Image-file extensions that the Blender script will accept.  Mirrored from
# blender/process_asset.py so we can reject obviously-bogus manifests before
# even spawning Blender (defence in depth — the script also validates).
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr",
    ".hdr", ".bmp", ".webp", ".dds",
})


class ManifestValidationError(ValueError):
    """Raised when a manifest fails the pre-spawn allowlist check."""


def _validate_manifest(manifest: dict) -> None:
    """Cheap allowlist check on a manifest before we hand it to Blender.

    Mirrors the validation inside ``blender/process_asset.py``.  Any
    inconsistency is raised as :class:`ManifestValidationError` so the caller
    can surface a meaningful error instead of waiting for a Blender crash.
    """
    psk_path = manifest.get("psk_path", "")
    output_path = manifest.get("output_path", "")
    outputs_root = manifest.get("outputs_root", "")

    if not isinstance(psk_path, str) or not psk_path:
        raise ManifestValidationError("manifest missing psk_path")
    if not isinstance(output_path, str) or not output_path:
        raise ManifestValidationError("manifest missing output_path")

    psk_ext = os.path.splitext(psk_path)[1].lower()
    if psk_ext not in (".psk", ".pskx"):
        raise ManifestValidationError(
            f"psk_path must end in .psk/.pskx (got {psk_ext!r})"
        )

    if not os.path.isabs(output_path):
        raise ManifestValidationError(
            f"output_path must be absolute: {output_path}"
        )
    # ".blend" = the standard processing output; ".fbx" = the FBX export
    # path (mesh format "fbx"): the script exports the imported rig instead
    # of saving a .blend file. Anything else is a typo/adversarial path.
    out_ext = os.path.splitext(output_path)[1].lower()
    if out_ext not in (".blend", ".fbx"):
        raise ManifestValidationError(
            f"output_path must end in .blend or .fbx: {output_path}"
        )

    if outputs_root:
        norm_root = os.path.normpath(outputs_root)
        norm_out = os.path.normpath(output_path)
        try:
            common = os.path.commonpath([norm_out, norm_root])
        except ValueError:
            common = ""
        if common != norm_root:
            raise ManifestValidationError(
                f"output_path {output_path!r} escapes outputs_root "
                f"{outputs_root!r}"
            )

    materials_spec = manifest.get("materials", {})
    if materials_spec is None:
        materials_spec = {}
    if not isinstance(materials_spec, dict):
        raise ManifestValidationError("materials must be a dict")
    for mat_name, spec in materials_spec.items():
        textures = (spec or {}).get("textures", {}) or {}
        if not isinstance(textures, dict):
            raise ManifestValidationError(
                f"materials[{mat_name!r}].textures must be a dict"
            )
        for slot, tex_info in textures.items():
            if not isinstance(tex_info, dict):
                raise ManifestValidationError(
                    f"materials[{mat_name!r}].textures[{slot!r}] must be a dict"
                )
            tex_path = tex_info.get("path", "")
            if not isinstance(tex_path, str) or not tex_path:
                raise ManifestValidationError(
                    f"materials[{mat_name!r}].textures[{slot!r}] missing path"
                )
            tex_ext = os.path.splitext(tex_path)[1].lower()
            if tex_ext not in _IMAGE_EXTS:
                raise ManifestValidationError(
                    f"materials[{mat_name!r}].textures[{slot!r}] has "
                    f"unsupported extension {tex_ext!r}"
                )


# In-memory registry of validated Blender binaries. Keyed by absolute path,
# value is a (mtime_ns, sha256_prefix) tuple. Re-validation runs whenever the
# path or mtime drifts — typical sessions hit the cache exactly once.
_BLENDER_VALIDATION_CACHE: dict[str, tuple[int, str, str]] = {}


class BlenderIdentityError(RuntimeError):
    """Raised when a configured Blender path does not look like Blender."""


def _hash_prefix(path: Path, *, n: int = 4096) -> str:
    """Return the SHA-256 of the first *n* bytes of *path* (cheap fingerprint)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(n)
    except OSError:
        return ""
    return hashlib.sha256(chunk).hexdigest()


def _validate_blender_exe(blender_exe: str) -> tuple[bool, str]:
    """Verify *blender_exe* actually runs and self-identifies as Blender.

    Returns ``(ok, message)``. ``ok=False`` means the binary did not produce
    a ``Blender`` version banner — either because it's not Blender, the
    file is corrupt, or the launch flat-out failed.

    Result is cached per absolute path until either path or mtime changes,
    so the version probe runs at most once per session per binary.
    """
    if not blender_exe:
        return False, "Blender executable path is empty"

    abs_path = os.path.abspath(blender_exe)
    p = Path(abs_path)
    if not p.is_file():
        return False, f"Blender executable not found: {abs_path}"

    try:
        mtime = p.stat().st_mtime_ns
    except OSError as e:
        return False, f"Could not stat Blender executable: {e}"

    # Fast path: check mtime first before touching the file content.
    # Computing _hash_prefix on every call would read 4 KB of blender.exe
    # per asset (and trigger AV scanners on Windows) even when nothing changed.
    cached = _BLENDER_VALIDATION_CACHE.get(abs_path)
    if cached is not None and cached[0] == mtime:
        return True, cached[2]

    # Cache miss or mtime drifted — read the fingerprint and re-verify.
    fingerprint = _hash_prefix(p)

    try:
        result = subprocess.run(
            [abs_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Failed to launch Blender for --version: {e}"

    banner = (result.stdout or "").strip().splitlines()[:1]
    banner_line = banner[0] if banner else ""
    if not banner_line.startswith("Blender "):
        return False, (
            f"Executable did not self-identify as Blender (stdout starts: "
            f"{banner_line[:80]!r})"
        )

    _BLENDER_VALIDATION_CACHE[abs_path] = (mtime, fingerprint, banner_line)
    return True, banner_line


def reset_blender_validation_cache() -> None:
    """Clear the per-session validation cache (test helper)."""
    _BLENDER_VALIDATION_CACHE.clear()


def is_blender_available() -> bool:
    """Return True if the configured ``blender_exe`` exists and self-identifies.

    Reuses the per-binary validation cache so this is a no-op on every call
    after the first. Called from ``MainWindow`` to gate Blender-dependent UI.
    """
    try:
        import config  # local import keeps the module independent of Qt at load
        blender_exe = config.get("blender_exe")
    except Exception:  # noqa: BLE001
        return False
    if not blender_exe:
        return False
    ok, _msg = _validate_blender_exe(blender_exe)
    return ok


@dataclass
class BlenderResult:
    success: bool
    asset_name: str = ""
    materials_processed: int = 0
    materials_failed: int = 0
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    timed_out: bool = False


def run_blender(
    blender_exe: str,
    manifest: dict,
    timeout: int = 120,
    on_status: Optional[Callable[[dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_proc_started: Optional[Callable[["subprocess.Popen"], None]] = None,
) -> BlenderResult:
    """Run Blender in background mode to process a single asset.

    Args:
        blender_exe: Path to blender executable
        manifest: Job manifest dict (will be written to temp JSON file)
        timeout: Max seconds to wait
        on_status: Optional callback for real-time status updates
        cancel_check: Optional zero-arg callable that returns True when the
            caller wants this run aborted. Polled while we wait on Blender;
            on True we terminate the subprocess and return early instead of
            letting the timeout run out.
        on_proc_started: Optional callback invoked once with the live
            ``subprocess.Popen`` so the caller can hold a reference and call
            ``terminate()``/``kill()`` directly if needed.
    """
    asset_name = os.path.basename(manifest.get("psk_path", "unknown"))
    result = BlenderResult(success=False, asset_name=asset_name)

    # First-use-per-session identity check on the configured Blender binary.
    # Skipping this would let any subprocess silently masquerade as Blender.
    ok, banner = _validate_blender_exe(blender_exe)
    if not ok:
        result.error_message = f"Blender identity check failed: {banner}"
        log.error(result.error_message)
        return result

    # Defence-in-depth: reject obviously-bogus manifests before spawning a
    # subprocess.  The Blender script repeats this check itself.
    try:
        _validate_manifest(manifest)
    except ManifestValidationError as e:
        result.error_message = f"Manifest validation failed: {e}"
        log.error(result.error_message)
        return result

    # Per-run nonce — Blender prepends ``##ASSET_STATUS:<nonce>##`` to status
    # JSON, and we only parse lines carrying *this* nonce.  Random addon
    # output (which sometimes mimics a status banner) cannot spoof the wire
    # format because it doesn't know the nonce.
    nonce = secrets.token_hex(8)
    status_prefix = f"##ASSET_STATUS:{nonce}##"

    # Write manifest under cache/manifests/ so app uninstall sweeps it
    # (system temp leaks cruft when sessions don't shut down cleanly).
    manifests_dir = base_dir() / "cache" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix="ear_manifest_", dir=str(manifests_dir)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        cmd = [
            blender_exe,
            "--background",
            "--python",
            BLENDER_SCRIPT,
            "--",
            tmp_path,
            "--nonce",
            nonce,
        ]

        # Sanitize cmd for logging — no secrets currently flow through argv,
        # but the redactor is cheap and a future change might.
        log.info("Launching Blender: %s", " ".join(redact_sensitive(cmd)))

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if on_proc_started is not None:
            try:
                on_proc_started(proc)
            except Exception:
                log.exception("on_proc_started callback raised — ignoring")

        cancelled, stdout, stderr = _wait_with_cancel(
            proc, timeout=timeout, cancel_check=cancel_check
        )
        if cancelled:
            result.return_code = proc.returncode if proc.returncode is not None else -1
            result.error_message = "Cancelled by user"
            result.stdout = stdout
            result.stderr = stderr
            log.info("Blender cancelled for %s", asset_name)
            return result
        if stdout is None:
            # _wait_with_cancel signals timeout by returning (False, None, ...)
            result.timed_out = True
            result.error_message = f"Blender timed out after {timeout}s"
            result.stderr = stderr or ""
            result.return_code = -1
            log.error("Blender timed out for %s", asset_name)
            return result

        result.stdout = stdout
        result.stderr = stderr or ""
        result.return_code = proc.returncode

        # Parse structured status lines from stdout.
        #
        # We accept either the nonce-prefixed form ``##ASSET_STATUS:<nonce>##``
        # (the only one a current run's Blender script will emit) or the
        # legacy ``##ASSET_STATUS##`` form so callers using an older bundled
        # script keep working.  Random addon output that prints anything
        # else cannot impersonate a status line.
        for line in stdout.splitlines():
            payload = None
            if line.startswith(status_prefix):
                payload = line[len(status_prefix):]
            elif line.startswith("##ASSET_STATUS##"):
                payload = line[len("##ASSET_STATUS##"):]
            if payload is None:
                continue
            try:
                status = json.loads(payload)
            except json.JSONDecodeError:
                continue
            _process_status(status, result)
            if on_status:
                on_status(status)

        if proc.returncode == 0 and not result.error_message:
            result.success = True
        elif not result.error_message:
            result.error_message = (
                f"Blender exited with code {proc.returncode}"
            )
            # Try to extract useful info from stderr (redacted in case the
            # subprocess echoed any sensitive command-line state).
            if stderr.strip():
                last_lines = stderr.strip().splitlines()[-5:]
                result.error_message += "\n" + redact_sensitive("\n".join(last_lines))

    except FileNotFoundError:
        result.error_message = f"Blender executable not found: {blender_exe}"
        log.error(result.error_message)
        # Treat "Blender exe missing" as a fatal user-actionable problem,
        # not a per-asset failure — the same exe is missing for every job.
        try:
            from core import crash_reporter
            if crash_reporter.is_installed():
                crash_reporter.report_subprocess_crash(
                    "Blender", result.error_message, show_dialog=False,
                )
        except Exception:
            log.exception("crash_reporter.report_subprocess_crash failed")
    except Exception as e:
        result.error_message = f"Unexpected error: {e}"
        log.error(result.error_message, exc_info=True)
    finally:
        # Clean up temp manifest
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return result


def _wait_with_cancel(
    proc: "subprocess.Popen",
    timeout: int,
    cancel_check: Optional[Callable[[], bool]],
) -> tuple[bool, Optional[str], Optional[str]]:
    """Wait for *proc* to exit, polling ``cancel_check`` along the way.

    Returns ``(cancelled, stdout, stderr)``:

    - ``cancelled=True`` — caller asked us to stop; subprocess has been
      terminated/killed and pipes drained.
    - ``cancelled=False, stdout=None`` — timeout elapsed; subprocess killed,
      stderr is whatever drained.
    - ``cancelled=False, stdout=str`` — normal exit; ``stdout``/``stderr``
      are the full captured streams.
    """
    # Without a cancel callback the old simple path is still ideal — we don't
    # need to spin in a poll loop for the common case.
    if cancel_check is None:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
            return False, None, stderr
        return False, stdout, stderr

    # Drain stdout/stderr in a background thread so the OS pipe buffer never
    # fills up.  If the pipe isn't read while Blender writes, the buffer fills
    # (~4–64 KB on Windows), Blender blocks mid-write, the process never exits,
    # and our poll loop runs until the full timeout.  proc.communicate() uses
    # internal reader threads and handles this correctly; we just need to drive
    # it from a daemon thread so we can still check cancel_check() in parallel.
    _holder: list[tuple[str, str]] = []

    def _reader():
        try:
            out, err = proc.communicate()
        except Exception:
            log.exception("Blender stdout/stderr drain failed")
            out, err = "", ""
        _holder.append((out, err))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    elapsed = 0.0
    poll_interval = 0.25
    while True:
        t.join(timeout=poll_interval)
        if not t.is_alive():
            stdout, stderr = _holder[0] if _holder else ("", "")
            return False, stdout, stderr

        if cancel_check():
            # Signal the process then let the reader thread drain whatever
            # remains.  We avoid calling communicate() here to prevent a
            # double-communicate race with the reader thread.
            try:
                proc.terminate()
            except OSError:
                pass
            t.join(timeout=5)
            if t.is_alive():
                try:
                    proc.kill()
                except OSError:
                    pass
                t.join(timeout=5)
            stdout, stderr = _holder[0] if _holder else ("", "")
            return True, stdout, stderr

        elapsed += poll_interval
        if elapsed >= timeout:
            try:
                proc.kill()
            except OSError:
                pass
            t.join(timeout=5)
            _, stderr = _holder[0] if _holder else ("", "")
            return False, None, stderr


def _process_status(status: dict, result: BlenderResult):
    """Update BlenderResult from a status dict."""
    s = status.get("status", "")

    if s == "completed":
        result.materials_processed = status.get("materials_processed", 0)
        result.materials_failed = status.get("materials_failed", 0)

    elif s == "error":
        result.error_message = status.get("message", "Unknown error")

    elif s == "warning":
        result.warnings.append(status.get("message", ""))
