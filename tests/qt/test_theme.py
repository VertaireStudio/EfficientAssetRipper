"""Tests for `gui.theme.apply` lifecycle hygiene."""

from __future__ import annotations

import pytest

from PySide6.QtWidgets import QApplication

import gui.theme as theme

pytestmark = pytest.mark.qt


@pytest.fixture
def app(qtbot):
    return QApplication.instance() or QApplication([])


def test_apply_no_op_when_scheme_unchanged(app):
    """Re-applying the same scheme should not rebuild the proxy style."""
    theme.apply(app, "Dusk")
    style_a = theme._current_style
    theme.apply(app, "Dusk")
    style_b = theme._current_style
    assert style_a is style_b


def test_apply_swaps_style_when_scheme_changes(app):
    theme.apply(app, "Dusk")
    style_a = theme._current_style
    theme.apply(app, "Bloom")
    style_b = theme._current_style
    assert style_a is not style_b
    assert theme.current_scheme_name() == "Bloom"


def test_fonts_loaded_flag_is_idempotent(app):
    """Calling _load_custom_fonts twice should not re-register fonts."""
    theme._FONTS_LOADED = False
    theme._load_custom_fonts()
    assert theme._FONTS_LOADED is True
    # Second call is a no-op.
    theme._load_custom_fonts()
    assert theme._FONTS_LOADED is True


def test_current_scheme_returns_dict(app):
    theme.apply(app, "Dusk")
    c = theme.current_scheme()
    assert isinstance(c, dict)
    assert "bg_dark" in c


def test_build_qss_contains_combo_arrow_rules(app, tmp_path, monkeypatch):
    from gui.color_schemes import get_scheme

    monkeypatch.setattr(theme, "_THEME_CACHE_DIR", tmp_path / "theme")
    qss = theme._build_qss(get_scheme("Dusk"))
    assert "QComboBox::down-arrow" in qss
    assert "QComboBox::down-arrow:disabled" in qss
    assert "QHeaderView::down-arrow" in qss
    assert "QHeaderView::up-arrow" in qss
    assert "QTreeView::branch:has-children:closed" in qss
    assert "QTreeView::branch:has-children:open" in qss
    svgs = list((tmp_path / "theme").glob("arrow_*.svg"))
    assert svgs
    for fp in svgs:
        assert f'url("{fp.as_posix()}")' in qss


def test_build_qss_contains_dock_separator_rules(app, tmp_path, monkeypatch):
    """Dock-area separators need an explicit size — the separator is the
    resize hit zone, and the style default is too thin to grab."""
    from gui.color_schemes import get_scheme

    monkeypatch.setattr(theme, "_THEME_CACHE_DIR", tmp_path / "theme")
    qss = theme._build_qss(get_scheme("Dusk"))
    assert "QMainWindow::separator" in qss
    assert "QMainWindow::separator:hover" in qss
    assert "QSplitter::handle:hover" in qss


def test_arrow_files_are_color_keyed(app, tmp_path, monkeypatch):
    """Different schemes must produce differently-named arrow files so the
    QSS image cache (keyed by URL) is busted on scheme switch."""
    from gui.color_schemes import get_scheme

    monkeypatch.setattr(theme, "_THEME_CACHE_DIR", tmp_path / "theme")
    theme._build_qss(get_scheme("Dusk"))
    theme._build_qss(get_scheme("Slate"))
    names = {fp.name for fp in (tmp_path / "theme").glob("arrow_down_*.svg")}
    assert len(names) >= 2


def test_build_qss_degrades_when_cache_unwritable(app, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(theme, "_THEME_CACHE_DIR", blocker / "theme")

    from gui.color_schemes import get_scheme

    qss = theme._build_qss(get_scheme("Dusk"))
    assert "QComboBox::down-arrow" not in qss
    assert "QComboBox {" in qss


# ---------------------------------------------------------------------------
# UI scale
# ---------------------------------------------------------------------------

@pytest.fixture
def restore_scale(app):
    """Reset the global UI scale (and the applied stylesheet) after the test."""
    yield
    theme.set_ui_scale(1.0)
    theme.apply(app)


def test_scale_qss_scales_px_and_pt():
    qss = "* { font-size: 10pt; padding: 4px 8px; }"
    assert theme._scale_qss(qss, 1.5) == "* { font-size: 15pt; padding: 6px 12px; }"


def test_scale_qss_preserves_hairlines_and_sign():
    qss = "QFrame { border: 1px solid red; margin: -5px 0; top: -1px; }"
    scaled = theme._scale_qss(qss, 0.5)
    # 1px borders must never vanish; negative offsets keep their sign.
    assert "border: 1px solid red" in scaled
    assert "margin: -3px 0" in scaled or "margin: -2px 0" in scaled
    assert "top: -1px" in scaled


def test_scale_qss_floors_font_size():
    assert theme._scale_qss("font-size: 9pt;", 0.5) == "font-size: 6pt;"


def test_set_ui_scale_clamps():
    theme.set_ui_scale(10.0)
    assert theme.ui_scale() == theme.UI_SCALE_MAX / 100.0
    theme.set_ui_scale(0.01)
    assert theme.ui_scale() == theme.UI_SCALE_MIN / 100.0
    theme.set_ui_scale(1.0)
    assert theme.ui_scale() == 1.0


def test_apply_rebuilds_when_scale_changes(app, restore_scale):
    theme.apply(app, "Dusk")
    style_a = theme._current_style
    theme.set_ui_scale(1.5)
    theme.apply(app, "Dusk")
    assert theme._current_style is not style_a
    assert "font-size: 15pt" in app.styleSheet()


def test_apply_no_op_when_scale_unchanged(app, restore_scale):
    theme.set_ui_scale(1.2)
    theme.apply(app, "Dusk")
    style_a = theme._current_style
    theme.apply(app, "Dusk")
    assert theme._current_style is style_a
