"""Physical-memory detection without third-party dependencies.

Used by the Settings dialog to show what the export memory policy resolves
to on this machine ("Exporter budget ≈ 6.0 GB of 12.1 GB RAM currently
free"). Detection is best-effort: :func:`total_bytes` and
:func:`available_bytes` return 0 when the platform can't be probed, and
callers then hide the hint instead of showing a wrong number.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess

log = logging.getLogger(__name__)

_GIB = 1024 * 1024 * 1024

_PROC_MEMINFO = "/proc/meminfo"


def total_bytes() -> int:
    """Total physical RAM in bytes, or 0 when detection fails."""
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_total_bytes()
        if system == "Linux":
            return _linux_total_bytes()
        if system == "Darwin":
            return _macos_total_bytes()
    except Exception as e:  # noqa: BLE001 — a failed probe just hides the hint
        log.debug("physical memory probe failed on %s: %s", system, e)
    return 0


def available_bytes() -> int:
    """RAM the OS can hand out right now without swapping, or 0 on failure.

    Mirrors the CLI's free-RAM probe (Program.ProbeAvailableMemoryBytes):
    Windows GlobalMemoryStatusEx ``ullAvailPhys``, Linux ``/proc/meminfo``
    ``MemAvailable`` (with a ``MemFree`` fallback for kernels too old to
    expose it), macOS best-effort free+inactive pages. 0 = unknown — the
    caller should then hide the hint rather than show a wrong number.
    """
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_available_bytes()
        if system == "Linux":
            return _linux_available_bytes()
        if system == "Darwin":
            return _macos_available_bytes()
    except Exception as e:  # noqa: BLE001 — a failed probe just hides the hint
        log.debug("available memory probe failed on %s: %s", system, e)
    return 0


def format_gb(n_bytes: int) -> str:
    """Human-scaled GB string, one decimal place."""
    return f"{n_bytes / _GIB:.1f} GB"


def budget_label(percent: int, physical_only: bool) -> str | None:
    """Describe the effective export budget in absolute terms.

    Returns ``None`` when RAM detection failed or the policy is effectively
    off (no percent, no physical-only) — the caller should then show nothing
    rather than a misleading figure.

    Mirrors the CLI's budget math (see Program.ResolveMemoryPolicy): the
    percentage applies to the RAM that is currently free. The CLI additionally
    adds its own resident footprint back to the free reading (it can shrink
    those pages between exports); this hint shows the raw free figure and so
    errs on the low side. Regardless of the percentage, physical-only clamps
    the budget to physical RAM minus an OS reserve (10%, floored at 512 MB),
    computed against the total.
    """
    total = total_bytes()
    if total <= 0:
        return None

    pct = max(0, min(95, int(percent)))
    if pct > 0:
        avail = available_bytes()
        if avail <= 0:
            return None
        budget = avail * pct // 100
        if physical_only:
            budget = min(budget, _physical_only_budget(total))
        return (
            f"Exporter budget ≈ {format_gb(budget)} "
            f"of {format_gb(avail)} RAM currently free"
        )

    if physical_only:
        return (
            "Unlimited up to physical RAM "
            f"(budget capped at ≈ {format_gb(_physical_only_budget(total))})"
        )

    return None


def _physical_only_budget(total: int) -> int:
    """Physical RAM minus the OS/GUI reserve (10%, floor 512 MB)."""
    reserve = max(512 << 20, total // 10)
    return total - reserve if total > reserve else total


def _windows_total_bytes() -> int:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return int(stat.ullTotalPhys)
    return 0


def _linux_total_bytes() -> int:
    # SC_PHYS_PAGES counts real physical pages (unlike the cgroup-filtered
    # view of /sys/fs/cgroup), matching the CLI's GC-based total.
    page_size = os.sysconf("SC_PAGE_SIZE")
    page_count = os.sysconf("SC_PHYS_PAGES")
    if page_size > 0 and page_count > 0:
        return int(page_size) * int(page_count)
    return 0


def _macos_total_bytes() -> int:
    out = subprocess.run(
        ["sysctl", "-n", "hw.memsize"],
        capture_output=True, text=True, timeout=5,
    )
    if out.returncode == 0:
        return int(out.stdout.strip())
    return 0


def _windows_available_bytes() -> int:
    # ullAvailPhys is what the kernel reports as allocatable without
    # severely impacting other applications (free + standby lists) — the
    # same notion of "available" as Linux MemAvailable.
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return int(stat.ullAvailPhys)
    return 0


def _linux_available_bytes() -> int:
    # MemAvailable is the kernel's own estimate of allocatable-without-
    # swapping RAM (free pages plus reclaimable cache); MemFree is the
    # fallback on kernels that predate the field (< 3.14).
    avail = memfree = 0
    try:
        with open(_PROC_MEMINFO, "r") as f:
            for line in f:
                if avail == 0 and line.startswith("MemAvailable:"):
                    avail = _parse_meminfo_kib(line)
                    if avail > 0:
                        return avail
                elif memfree == 0 and line.startswith("MemFree:"):
                    memfree = _parse_meminfo_kib(line)
    except OSError:
        return 0
    return max(avail, memfree)


def _parse_meminfo_kib(line: str) -> int:
    """Parse the ``value kB`` tail of a ``/proc/meminfo`` line into bytes."""
    try:
        kib = int(line.split(":", 1)[1].strip().split(" ", 1)[0])
        return kib * 1024
    except (IndexError, ValueError):
        return 0


def _macos_available_bytes() -> int:
    # macOS has no clean "available RAM" API; free + inactive pages is the
    # usual approximation (it undercounts dirty anon pages back in the free
    # list, erring low rather than promising more RAM than exists).
    page = _sysctl_int("vm.pagesize") or _sysctl_int("hw.pagesize")
    if page <= 0:
        return 0
    free = _sysctl_int("vm.page_free_count")
    inactive = _sysctl_int("vm.page_inactive_count")
    total = (free + inactive) * page
    return total if total > 0 else 0


def _sysctl_int(name: str) -> int:
    out = subprocess.run(
        ["sysctl", "-n", name],
        capture_output=True, text=True, timeout=5,
    )
    if out.returncode == 0:
        try:
            return int(out.stdout.strip())
        except ValueError:
            return 0
    return 0
