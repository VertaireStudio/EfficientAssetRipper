"""Tests for `core.memory_info` — physical RAM detection and budget labels."""

from __future__ import annotations

import pytest

from core import memory_info

pytestmark = pytest.mark.unit


def test_total_bytes_positive_or_zero():
    """On supported platforms detection should succeed; elsewhere return 0."""
    total = memory_info.total_bytes()
    assert total == 0 or total > (1 << 30)


def test_available_bytes_positive_or_zero():
    """On supported platforms the probe fails soft; else returns 0."""
    avail = memory_info.available_bytes()
    total = memory_info.total_bytes()
    assert avail >= 0
    # When both readings succeed, "free" can never exceed the total.
    if avail and total:
        assert avail <= total


def test_physical_only_budget_reserves_at_least_512mb():
    assert memory_info._physical_only_budget(1 << 30) == (1 << 30) - (512 << 20)
    # 10% of 16 GB = 1.6 GB reserve wins over the 512 MB floor.
    assert memory_info._physical_only_budget(16 * 1024**3) == 16 * 1024**3 - 16 * 1024**3 // 10


def test_budget_label_percent_of_free(monkeypatch):
    """The percentage applies to the RAM currently free, not the total."""
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(memory_info, "available_bytes", lambda: 8 * 1024**3)
    label = memory_info.budget_label(50, physical_only=False)
    assert label is not None
    assert "4.0 GB" in label and "8.0 GB" in label and "currently free" in label


def test_budget_label_percent_cannot_exceed_free_share(monkeypatch):
    """Nothing in the label may cite the total when the basis is free RAM."""
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(memory_info, "available_bytes", lambda: 8 * 1024**3)
    label = memory_info.budget_label(50, physical_only=False)
    assert "16.0 GB" not in label


def test_budget_label_physical_only_clamps_percent(monkeypatch):
    """When both settings are on, the tighter (physical-only) budget wins."""
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(memory_info, "available_bytes", lambda: 16 * 1024**3)
    label = memory_info.budget_label(95, physical_only=True)
    assert label is not None
    # 95% = 15.2 GB but physical-only reserves 10% → budget = 14.4 GB.
    assert "14.4 GB" in label


def test_budget_label_physical_only_without_percent(monkeypatch):
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 8 * 1024**3)
    label = memory_info.budget_label(0, physical_only=True)
    assert label is not None
    assert "7.2 GB" in label  # 90% of 8 GB


def test_budget_label_none_when_free_unknown(monkeypatch):
    """A percentage can't be resolved without a free-RAM reading."""
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(memory_info, "available_bytes", lambda: 0)
    assert memory_info.budget_label(50, physical_only=False) is None


def test_budget_label_none_when_policy_off(monkeypatch):
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 8 * 1024**3)
    assert memory_info.budget_label(0, physical_only=False) is None


def test_budget_label_none_when_ram_unknown(monkeypatch):
    monkeypatch.setattr(memory_info, "total_bytes", lambda: 0)
    assert memory_info.budget_label(50, physical_only=True) is None


def test_linux_available_bytes_prefers_memavailable(tmp_path, monkeypatch):
    """MemAvailable wins when present; MemFree is the < 3.14 fallback."""
    modern = tmp_path / "meminfo_modern"
    modern.write_text(
        "MemTotal:       16384000 kB\n"
        "MemFree:        21001236 kB\n"
        "MemAvailable:   14200000 kB\n"
    )
    monkeypatch.setattr(memory_info, "_PROC_MEMINFO", str(modern))
    assert memory_info._linux_available_bytes() == 14200000 * 1024

    ancient = tmp_path / "meminfo_ancient"
    ancient.write_text(
        "MemTotal:       16384000 kB\n"
        "MemFree:        21001236 kB\n"
    )
    monkeypatch.setattr(memory_info, "_PROC_MEMINFO", str(ancient))
    assert memory_info._linux_available_bytes() == 21001236 * 1024


def test_parse_meminfo_kib_handles_bad_lines():
    assert memory_info._parse_meminfo_kib("MemAvailable:   12345 kB") == 12345 * 1024
    assert memory_info._parse_meminfo_kib("Garbage") == 0
    assert memory_info._parse_meminfo_kib("MemAvailable:") == 0
    assert memory_info._parse_meminfo_kib("MemAvailable:  xyz kB") == 0


def test_format_gb():
    assert memory_info.format_gb(4 * 1024**3) == "4.0 GB"
    assert memory_info.format_gb(int(0.5 * 1024**3)) == "0.5 GB"
