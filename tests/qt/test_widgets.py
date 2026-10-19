"""Smoke tests for `gui.widgets` custom widgets."""

from __future__ import annotations

import pytest

from PySide6.QtWidgets import QVBoxLayout, QLabel

from gui.widgets import CollapsibleSection, LabeledSlider, PathPicker, ZoomableTree

pytestmark = pytest.mark.qt


def test_zoomable_tree_constructs(qtbot):
    t = ZoomableTree()
    qtbot.addWidget(t)
    assert t.font().pointSize() > 0


def test_collapsible_section_starts_expanded(qtbot):
    sec = CollapsibleSection("My Section", start_expanded=True, closeable=True)
    qtbot.addWidget(sec)
    assert sec._expanded is True
    assert sec._content.isVisibleTo(sec) or not sec.isVisible()  # hidden until shown


def test_collapsible_section_toggle_collapses(qtbot):
    sec = CollapsibleSection("Toggle Me", start_expanded=True, closeable=True)
    qtbot.addWidget(sec)
    sec._toggle()
    assert sec._expanded is False


def test_collapsible_section_set_content_layout(qtbot):
    sec = CollapsibleSection("With Content", parent=None)
    qtbot.addWidget(sec)
    inner = QVBoxLayout()
    inner.addWidget(QLabel("hello"))
    sec.set_content_layout(inner)
    assert sec._content.layout() is inner


def test_path_picker_setText_text_round_trip(qtbot):
    p = PathPicker(mode="folder")
    qtbot.addWidget(p)
    p.setText(r"C:\Picked")
    assert p.text() == r"C:\Picked"


def test_path_picker_emits_changed_signal(qtbot):
    p = PathPicker(mode="folder")
    qtbot.addWidget(p)
    with qtbot.waitSignal(p.changed, timeout=1000) as sig:
        p.setText(r"C:\NewPath")
    assert sig.args == [r"C:\NewPath"]


def test_path_picker_browse_file_mode_uses_open_file_dialog(qtbot, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    captured: dict = {}

    def _fake(parent, title, start, filt):
        captured["called"] = (parent, title, start, filt)
        return (r"C:\From\Mock\file.exe", filt)

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(_fake))
    p = PathPicker(mode="file", filter_str="Exe (*.exe)")
    qtbot.addWidget(p)
    p._browse()
    assert captured.get("called") is not None
    assert p.text() == r"C:\From\Mock\file.exe"


# ---------------------------------------------------------------------------
# LabeledSlider — slider + live value caption standing in for QSpinBox
# ---------------------------------------------------------------------------

def test_labeled_slider_value_round_trip_and_caption(qtbot):
    s = LabeledSlider(0, 95, value=75, suffix=" %")
    qtbot.addWidget(s)
    assert s.minimum() == 0
    assert s.maximum() == 95
    assert s.value() == 75
    assert s.label_text == "75 %"


def test_labeled_slider_special_text_only_at_minimum(qtbot):
    s = LabeledSlider(0, 95, value=0, suffix=" %", special_text="Unlimited")
    qtbot.addWidget(s)
    assert s.specialValueText() == "Unlimited"
    assert s.label_text == "Unlimited"
    s.setValue(10)
    assert s.label_text == "10 %"


def test_labeled_slider_snaps_to_step(qtbot):
    """Dragging maps groove positions to the step grid (UI-scale style)."""
    s = LabeledSlider(50, 200, step=10, value=50, suffix="%")
    qtbot.addWidget(s)
    s.setValue(57)          # half-up snap onto the grid
    assert s.value() == 60
    s.setValue(50)
    assert s.value() == 50
    s.setValue(192)         # slotted to the top regular step…
    assert s.value() == 190
    s.setValue(200)         # …the pinned end stop still reaches max
    assert s.value() == 200
    s.setValue(999)         # clamping on both sides
    assert s.value() == 200
    s.setValue(-5)
    assert s.value() == 50


def test_labeled_slider_emits_value_changed_once(qtbot):
    s = LabeledSlider(0, 95, value=0)
    qtbot.addWidget(s)
    with qtbot.waitSignal(s.valueChanged, timeout=1000) as sig:
        s.setValue(42)
    assert sig.args == [42]

    emissions: list[int] = []
    s.valueChanged.connect(emissions.append)
    s.setValue(42)          # QSpinBox parity: same value → no re-emit
    assert emissions == []


def test_labeled_slider_set_range_and_step(qtbot):
    s = LabeledSlider(0, 100, value=90)
    qtbot.addWidget(s)
    s.setRange(0, 94, step=5)   # non-divisible span (94 % 5) pins the end stop
    assert s.minimum() == 0
    assert s.maximum() == 94
    s.setValue(92)              # above the top regular step → pinned max stop
    assert s.value() == 94
    s.setValue(88)              # below it → snaps back onto the grid
    assert s.value() == 90

    s.setSingleStep(10)
    assert s._step == 10        # re-derived tick geometry
    s.setValue(94)              # the pinned top stop stays reachable
    assert s.value() == 94


def test_labeled_slider_tooltip_propagates_to_slider(qtbot):
    s = LabeledSlider(1, 100, value=90)
    qtbot.addWidget(s)
    s.setToolTip("Lossy WebP quality.")
    assert s._slider.toolTip() == "Lossy WebP quality."
