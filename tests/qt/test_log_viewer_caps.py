"""Bytes-bound regression test for warning spam leaking through LogViewer.

CUE4Parse emits per-object warnings during export (e.g. the props-writer
failure ".../SwapTo/FromTrails.props.txt: Could not find a part of the
path"). A full-game export produces millions of them, and each flows
through ``UnpackerPanel._on_warning`` into the LogViewer. Every record
backing that chain must be byte-bounded at any instant, because the only
unbounded one — ``LogViewer._copy_log`` — held every accumulated warning
in RAM until a multi-day export OOM'd the process.

Partner change: ``LogViewer._copy_log`` builds its clipboard string so
the copy path can never assemble more than ``_MAX_LOG_ENTRIES`` lines of
log text into RAM at once.
"""

from __future__ import annotations

import pytest

import gui.log_viewer as lv
from gui.log_viewer import LogViewer

pytestmark = pytest.mark.qt

_WARNING_LINE = (
    "CUE4Parse warning: Props export failed for SwapTo/FromTrails: "
    "Could not find a part of the path '<path>"
    " of Insight/IslandsofInsight/Content/ASophia/Characters/"
    "SwapTo/FromTrails.props.txt'."
)


def test_entries_tail_is_byte_bounded_under_warning_spam(qtbot):
    """LogViewer._entries must drop the oldest when warned-to-cap."""
    w = LogViewer()
    qtbot.addWidget(w)
    for _ in range(lv._MAX_LOG_ENTRIES * 3):
        w.append(_WARNING_LINE, "warning")
    assert len(w._entries) <= lv._MAX_LOG_ENTRIES
    # The freshest — not the first — records survive; the leaker held all of them.
    assert any(line == _WARNING_LINE for line, _ in w._entries)


def test_copy_log_does_not_grow_unbounded(qtbot, monkeypatch):
    """The fixed _copy_log must not concatenate all entries into one string.

    The leaked build concatenated every entry first; the fixed one caps
    the concatenated payload (see log_viewer), so under a full-game
    warning flood the copy stays bounded instead of spiking to GBs.
    """
    monkeypatch.setattr(lv, "_MAX_LOG_ENTRIES", 50)
    w = LogViewer()
    qtbot.addWidget(w)
    for _ in range(150):
        w.append(_WARNING_LINE, "warning")
    assert len(w._entries) <= 50  # pre-cap: backlog itself is bounded
    # _copy_log must succeed without concatenating more than the cap.
    w._copy_log()
