"""Animated splash overlay for EfficientAssetRipper.

Sequence
--------
1. Logo gem spins up from the bottom
2. Circle grows around the logo
3. Logo + circle hold for a beat
4. Circle accelerates outward in one motion, squaring into a
   rectangle that covers the window
5. The whole cover slides up and off, revealing the UI

The splash is a *child widget* of the main window, not a separate OS
window. A single toplevel means Windows never has to swap between two
surfaces when the animation ends — closing a second stay-on-top window
sometimes left a frame of desktop visible, which looked like the app
closing and relaunching. Unpainted regions of the overlay show the UI
underneath, which is what makes the final slide-away reveal work.

Progress is driven by ``QElapsedTimer`` (real wall-clock time) on a
fixed-interval ``QTimer``. If the GUI thread stutters and frames drop,
progress just resyncs to elapsed time on the next tick — it can never
"skip ahead" past the visible portion the way a QPropertyAnimation
does when its valueChanged signal isn't drained promptly.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QWidget

from _version import __version__
from gui.color_schemes import get_scheme
import gui.theme as theme


# ---------------------------------------------------------------------------
# Animation phases encoded as a single 0‒1 float (``progress``)
#
#   0.00 – 0.41  Logo rise + spin                 (1000 ms)
#   0.41 – 0.53  Circle grows around logo         (300 ms)
#   0.53 – 0.67  Hold on logo + circle            (350 ms)
#   0.67 – 0.84  Circle accelerates out to cover, squaring into a rectangle
#   0.84 – 1.00  Cover slides up, revealing the UI
# ---------------------------------------------------------------------------

_PHASE_RISE_END = 0.41
_PHASE_CIRCLE_END = 0.53
_PHASE_HOLD_END = 0.67
_PHASE_EXPAND_END = 0.84
_PHASE_SLIDE_END = 1.00

_DURATION_MS = 2450            # total animation length
_FRAME_INTERVAL_MS = 16        # ~60 FPS target


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _ease_in_cubic(t: float) -> float:
    return t ** 3


def _ease_in_out(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    else:
        return 1 - (-2 * t + 2) ** 3 / 2


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


class SplashScreen(QWidget):
    """Animated overlay covering the main window's client area."""

    def __init__(
        self,
        parent: QWidget,
        finish_callback: Callable[[], None] | None = None,
        scheme_name: str | None = None,
    ):
        super().__init__(parent)
        self._finish_cb = finish_callback
        # Match the applied theme so the cover blends into the revealed UI.
        self._colors = get_scheme(scheme_name) if scheme_name else theme.current_scheme()

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._progress = 0.0       # 0→1 drives the whole sequence
        self._finished = False

        # Animation: real-time clock + fixed-interval repaint.
        self._elapsed = QElapsedTimer()
        self._tick = QTimer(self)
        self._tick.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick.setInterval(_FRAME_INTERVAL_MS)
        self._tick.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self):
        """Cover the parent window and kick off the animation."""
        parent = self.parentWidget()
        self.setGeometry(parent.rect())
        # Track parent resizes (e.g. a maximized saved layout realising
        # after show) so the cover never leaves an uncovered strip.
        parent.installEventFilter(self)
        self.show()
        self.raise_()
        self._elapsed.start()
        self._tick.start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
        return False

    def _on_tick(self):
        # Recompute progress from elapsed wall-clock time. Frame drops just
        # mean fewer paints — they cannot cause the animation to "skip" or
        # end early, because progress is purely a function of elapsed ms.
        elapsed = self._elapsed.elapsed()
        self._progress = min(elapsed / float(_DURATION_MS), 1.0)
        self.update()
        if self._progress >= 1.0:
            self._tick.stop()
            self._on_animation_done()

    def _on_animation_done(self):
        if self._finished:
            return
        self._finished = True
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        try:
            if self._finish_cb is not None:
                self._finish_cb()
        finally:
            # Always close the splash, even if the callback raises;
            # leaving an invisible overlay around blocks input on the
            # widgets underneath.
            self.close()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W = float(self.width())
        H = float(self.height())
        cx, cy = W / 2, H / 2
        t = self._progress

        c = self._colors
        gem_size = min(W, H) * 0.08
        ring_r = gem_size * 2.5

        # ── Phase 5: slide the whole cover up and off ────────────────
        # Everything below the translated cover is simply not painted,
        # so the main window shows through — the reveal *is* the slide.
        if t >= _PHASE_EXPAND_END:
            slide_t = _ease_out_cubic((t - _PHASE_EXPAND_END) / (_PHASE_SLIDE_END - _PHASE_EXPAND_END))
            p.translate(0, -slide_t * H)
            p.fillRect(QRectF(0, 0, W, H), QColor(c["bg_dark"]))
            self._draw_gem(p, cx, cy, gem_size, spin_deg=0.0)
            self._draw_footer(p, W, H, alpha=200)
            p.end()
            return

        # ── Phases 1-4: backdrop + gem + circle/bar ──────────────────
        p.fillRect(QRectF(0, 0, W, H), QColor(c["bg_darkest"]))

        rise_t = _ease_out_cubic(min(t / _PHASE_RISE_END, 1.0))
        logo_y = _lerp(H * 0.85, cy, rise_t)
        spin_deg = rise_t * 360.0

        # One continuous shape morph: a centred rounded rect that starts as
        # the circle (radius == both half-extents) and expands toward every
        # screen edge at once while its corner radius shrinks to zero — so
        # it squares into a rectangle as it grows.
        if t >= _PHASE_RISE_END:
            if t < _PHASE_CIRCLE_END:
                grow_t = _ease_out_cubic((t - _PHASE_RISE_END) / (_PHASE_CIRCLE_END - _PHASE_RISE_END))
                half_w = half_h = _lerp(0, ring_r, grow_t)
                radius = half_h
                alpha = int(_lerp(0, 200, grow_t))
            elif t < _PHASE_HOLD_END:
                half_w = half_h = radius = ring_r
                alpha = 200
            else:
                # Ease-in: starts from the held pose at rest and accelerates
                # outward; the slide phase's ease-out then carries the
                # momentum through to the reveal.
                expand_t = _ease_in_cubic((t - _PHASE_HOLD_END) / (_PHASE_EXPAND_END - _PHASE_HOLD_END))
                half_w = _lerp(ring_r, W / 2 + gem_size, expand_t)
                half_h = _lerp(ring_r, H / 2 + gem_size, expand_t)
                radius = _lerp(ring_r, 0.0, expand_t)
                alpha = 200

            if half_w > 0 and half_h > 0:
                rect = QRectF(cx - half_w, cy - half_h, half_w * 2, half_h * 2)

                fill = QColor(c["bg_dark"])
                fill.setAlpha(int(alpha * 255 / 200))
                edge = QColor(c["accent"])
                edge.setAlpha(alpha)
                glow = QColor(c["accent"])
                glow.setAlpha(alpha // 3)

                p.setPen(QPen(glow, max(4.0, gem_size * 0.18)))
                p.setBrush(Qt.BrushStyle.NoBrush)
                g = gem_size * 0.08
                p.drawRoundedRect(rect.adjusted(-g, -g, g, g), radius + g, radius + g)

                p.setPen(QPen(edge, max(2.0, gem_size * 0.08)))
                p.setBrush(QBrush(fill))
                p.drawRoundedRect(rect, radius, radius)

        self._draw_gem(p, cx, logo_y, gem_size, spin_deg)
        self._draw_footer(p, W, H, alpha=int(_lerp(0, 200, min(t / _PHASE_RISE_END, 1.0))))
        p.end()

    def _draw_gem(self, p: QPainter, x: float, y: float, size: float, spin_deg: float):
        c = self._colors
        p.save()
        p.translate(x, y)
        p.rotate(spin_deg)

        dh = size * 0.9
        dw = size * 0.65

        gem = QPolygonF([
            QPointF(0, -dh),
            QPointF(dw, 0),
            QPointF(0, dh),
            QPointF(-dw, 0),
        ])

        gem_grad = QLinearGradient(-dw, -dh, dw, dh)
        gem_grad.setColorAt(0.0, QColor(c["accent"]))
        gem_grad.setColorAt(0.5, QColor(c["accent_hover"]))
        gem_grad.setColorAt(1.0, QColor(c["accent_muted"]))
        p.setBrush(QBrush(gem_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(gem)

        # Facet lines are clipped to the silhouette so stroke width never
        # pokes past the gem's edges.
        clip = QPainterPath()
        clip.addPolygon(gem)
        clip.closeSubpath()
        p.save()
        p.setClipPath(clip)
        lw = max(1.0, size * 0.04)
        p.setPen(QPen(QColor(255, 255, 255, 170), lw))
        p.drawLine(QPointF(0, -dh), QPointF(-dw * 0.5, 0))
        p.drawLine(QPointF(0, -dh), QPointF(dw * 0.5, 0))
        p.setPen(QPen(QColor(255, 255, 255, 70), lw * 0.8))
        p.drawLine(QPointF(-dw, 0), QPointF(dw, 0))
        p.drawLine(QPointF(0, dh), QPointF(-dw * 0.5, 0))
        p.drawLine(QPointF(0, dh), QPointF(dw * 0.5, 0))
        p.restore()

        p.restore()

    def _draw_footer(self, p: QPainter, W: float, H: float, alpha: int):
        footer = QColor(self._colors["text_secondary"])
        footer.setAlpha(alpha)
        font = QFont()
        font.setPointSizeF(max(8.0, min(W, H) * 0.012))
        p.setFont(font)
        p.setPen(QPen(footer))
        p.drawText(
            QRectF(0, H - max(28.0, H * 0.06), W, 24.0),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            f"EfficientAssetRipper v{__version__}",
        )
