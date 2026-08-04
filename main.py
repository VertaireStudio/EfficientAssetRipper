"""EfficientAssetRipper — Unpack UE4/5 game files and export to Blender."""

import sys
import logging

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen,
    QLinearGradient, QPainterPath, QPolygonF,
)

from gui.main_window import MainWindow
from gui.splash import SplashScreen
import gui.theme as theme
import config

# ── Toggle the startup animation on/off ──────────────────────────────────────
SHOW_SPLASH = True


def _make_icon() -> QIcon:
    """Gem/crystal icon rendered at multiple sizes for crisp display."""
    c = theme.current_scheme()
    icon = QIcon()

    for size in (16, 32, 48, 64, 256):
        px = QPixmap(size, size)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        s = float(size)
        cx, cy = s / 2, s / 2
        pad = s * 0.05
        radius = s * 0.18

        # --- dark rounded background ---
        bg = QLinearGradient(0.0, 0.0, s, s)
        bg.setColorAt(0.0, QColor(c["bg_darkest"]))
        bg.setColorAt(1.0, QColor(c["bg_dark"]))
        p.setBrush(QBrush(bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(pad, pad, s - 2 * pad, s - 2 * pad), radius, radius)

        # --- gem diamond shape ---
        dh = s * 0.36   # half-height
        dw = s * 0.27   # half-width

        gem = QPolygonF([
            QPointF(cx,        cy - dh),   # top
            QPointF(cx + dw,   cy),         # right
            QPointF(cx,        cy + dh),   # bottom
            QPointF(cx - dw,   cy),         # left
        ])
        gem_grad = QLinearGradient(cx - dw, cy - dh, cx + dw, cy + dh)
        gem_grad.setColorAt(0.0, QColor(c["accent"]))
        gem_grad.setColorAt(0.45, QColor(c["accent_hover"]))
        gem_grad.setColorAt(1.0,  QColor(c["accent_muted"]))
        p.setBrush(QBrush(gem_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(gem)

        # --- inner facet lines (only worth drawing at >=32 px) ---
        # Clipped to the gem silhouette so stroke width never pokes past
        # the edges, and symmetric so the gem doesn't look lopsided.
        if size >= 32:
            clip = QPainterPath()
            clip.addPolygon(gem)
            clip.closeSubpath()
            p.save()
            p.setClipPath(clip)
            lw = max(1.0, s * 0.025)
            p.setPen(QPen(QColor(255, 255, 255, 170), lw))
            p.drawLine(QPointF(cx, cy - dh), QPointF(cx - dw * 0.5, cy))
            p.drawLine(QPointF(cx, cy - dh), QPointF(cx + dw * 0.5, cy))
            p.setPen(QPen(QColor(255, 255, 255, 70), lw * 0.8))
            p.drawLine(QPointF(cx - dw, cy), QPointF(cx + dw, cy))
            p.drawLine(QPointF(cx, cy + dh), QPointF(cx - dw * 0.5, cy))
            p.drawLine(QPointF(cx, cy + dh), QPointF(cx + dw * 0.5, cy))
            p.restore()

        # --- outer glow ring ---
        # Kept small enough (scale * dh + pen/2 < tile half-extent) that it
        # never crosses the rounded background's edge.
        if size >= 32:
            gw = max(1.5, s * 0.045)
            glow_pen = QPen(QColor(c["accent"]).lighter(120), gw)
            glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(glow_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            scale = 1.15
            p.drawPolygon(QPolygonF([
                QPointF(cx,              cy - dh * scale),
                QPointF(cx + dw * scale, cy),
                QPointF(cx,              cy + dh * scale),
                QPointF(cx - dw * scale, cy),
            ]))

        p.end()
        icon.addPixmap(px)

    return icon


def _load_saved_custom_schemes():
    """Restore user-defined colour schemes from QSettings/config."""
    import json
    from gui.color_schemes import register_custom_scheme
    raw = config.get("custom_schemes")
    if not raw or raw == "{}":
        return
    try:
        custom = json.loads(raw)
        for name, colors in custom.items():
            register_custom_scheme(name, colors)
    except (json.JSONDecodeError, TypeError) as e:
        logging.getLogger(__name__).warning(
            "Failed to parse custom_schemes from config: %s", e
        )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Strip AES keys / hex blobs from any log record before it's emitted.
    from core.log_redaction import install_global_redactor
    install_global_redactor()

    # Catch otherwise-uncaught exceptions and Qt fatal messages, write a
    # report under logs/, and offer the user a one-click bug filing path.
    # Must come AFTER install_global_redactor so report contents share the
    # same redaction filter as ordinary log lines.
    from core import crash_reporter
    crash_reporter.install(
        active_profile_provider=lambda: config.get("active_profile") or "",
    )

    # Windows: give this process its own identity so the taskbar shows
    # our icon instead of the generic Python interpreter icon.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EfficientAssetRipper.App.1"
        )

    app = QApplication(sys.argv)
    app.setApplicationName("EfficientAssetRipper")
    app.setOrganizationName("EfficientAssetRipper")

    # Apply centralised theme (palette + stylesheet)
    _load_saved_custom_schemes()
    saved_scheme = config.get("color_scheme") or None
    try:
        theme.set_ui_scale(config.get_int("ui_scale") / 100.0)
    except (TypeError, ValueError):
        pass  # corrupt registry value — stay at 100%
    theme.apply(app, saved_scheme)

    icon = _make_icon()
    app.setWindowIcon(icon)

    # Build the main window (hidden) so it loads during the splash. Geometry
    # restoration runs inside MainWindow.__init__ — it falls back to a centred
    # 1600×950 default when no saved layout exists or the schema mismatches.
    window = MainWindow()
    window.setWindowIcon(icon)

    if SHOW_SPLASH:
        # The splash is a child overlay inside the main window, not a second
        # OS window. Creating it before show() means the window's very first
        # presented frame is already the splash cover — no flash of bare UI
        # on launch, and nothing to close/hand off when the animation ends.
        splash = SplashScreen(parent=window)
        window.show()
        splash.start()
    else:
        window.show()

    try:
        rc = app.exec()
    except Exception:
        # The crash reporter's excepthook already wrote a report and showed
        # the dialog before this re-raise reaches us. We just have to make
        # sure we exit non-zero so launchers / CI know something went wrong.
        logging.getLogger(__name__).exception("Fatal error in main event loop")
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
