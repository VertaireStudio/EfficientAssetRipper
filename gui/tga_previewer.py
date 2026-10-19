"""TGA image previewer widget with drag-and-drop and zoom."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QWheelEvent, QDragEnterEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

from PIL import Image as PILImage


def _wipe_temp_dir(path: Path) -> None:
    """atexit hook — remove the temp dir, swallowing all errors."""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

# Force PIL plugin discovery on the GUI thread up front. PIL's `preinit()`
# imports image-format plugin modules lazily on first `Image.open()`, and
# Python's import lock isn't thread-safe under the QThreadPool worker model
# we use below — running it cold from a worker can crash with an access
# violation on Windows.
try:
    PILImage.preinit()
except Exception:
    pass


# Drag-accept and load-time recognition cover most texture formats CUE4Parse
# emits — keep these in sync with `_IMAGE_EXTS` in unpacker_panel.
_SUPPORTED_EXTS = (".tga", ".png", ".jpg", ".jpeg", ".bmp", ".dds", ".webp")


class _ImageLoadSignals(QObject):
    """QRunnable can't subclass QObject directly — split signals out."""

    loaded = Signal(int, QImage, int, int, str)  # token, image, w, h, name
    failed = Signal(int, str, str)               # token, name, error


class _ImageLoadRunnable(QRunnable):
    """Decode an image off the GUI thread."""

    def __init__(self, token: int, path: str, signals: _ImageLoadSignals):
        super().__init__()
        self._token = token
        self._path = path
        self._signals = signals

    def run(self):
        name = Path(self._path).name
        try:
            pil_img = PILImage.open(self._path)
            pil_img = pil_img.convert("RGBA")
            data = pil_img.tobytes("raw", "RGBA")
            img = QImage(data, pil_img.width, pil_img.height, QImage.Format.Format_RGBA8888)
            # QImage doesn't own the buffer, so copy it.
            img = img.copy()
        except Exception as e:
            self._signals.failed.emit(self._token, name, str(e))
            return
        self._signals.loaded.emit(self._token, img, img.width(), img.height(), name)


class TGAPreviewerPanel(QWidget):
    """Drag-and-drop TGA previewer with zoom."""

    log_message = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel("Drop an image here to preview (TGA, PNG, JPG, BMP, DDS)")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

        self._view = _ZoomGraphicsView()
        self._view.setVisible(False)
        layout.addWidget(self._view)

        self._scene = QGraphicsScene(self)
        self._view.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None

        # Background loader — wired once, reused for every load.
        self._load_token = 0
        self._load_signals = _ImageLoadSignals()
        self._load_signals.loaded.connect(self._on_load_done)
        self._load_signals.failed.connect(self._on_load_failed)

        # Temp dir for unpacker preview-only exports — same pattern as the
        # audio previewer. Lets the user peek at textures without writing
        # them into the configured export folder.
        self._temp_dir = Path(tempfile.mkdtemp(prefix="ear_tga_preview_"))
        atexit.register(_wipe_temp_dir, self._temp_dir)

    @property
    def temp_dir(self) -> Path:
        """Temp directory the unpacker can drop preview-only textures into."""
        return self._temp_dir

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(_SUPPORTED_EXTS):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(_SUPPORTED_EXTS):
                self._load_image(path)
                event.acceptProposedAction()
                return

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_file(self, path: str):
        """Load an image file programmatically (supports TGA, PNG, DDS, etc.)."""
        self._load_image(path)

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------

    def _load_image(self, path: str):
        # Token bumps invalidate any in-flight previous load; the QRunnable
        # checks self._load_token before applying its result.
        self._load_token += 1
        token = self._load_token
        self._label.setText(f"Loading: {Path(path).name}...")
        self._label.setVisible(True)
        self._view.setVisible(False)

        runnable = _ImageLoadRunnable(token, path, self._load_signals)
        QThreadPool.globalInstance().start(runnable)

    def _on_load_done(self, token: int, img: QImage, w: int, h: int, name: str):
        if token != self._load_token:
            return  # superseded by a newer request

        pixmap = QPixmap.fromImage(img)

        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(pixmap.rect().toRectF())

        self._label.setVisible(False)
        self._view.setVisible(True)
        self._view.resetTransform()
        self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

        self._view.set_status_text(f"{name} | {w} x {h}")

    def _on_load_failed(self, token: int, name: str, error: str):
        if token != self._load_token:
            return
        self._label.setText(f"Failed to load: {name}\n{error}")
        self._label.setVisible(True)
        self._view.setVisible(False)


class _ZoomGraphicsView(QGraphicsView):
    """QGraphicsView with scroll-wheel zoom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setAcceptDrops(False)  # Let parent handle drops
        self._panning = False

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            # Re-send as left-click so ScrollHandDrag activates
            fake = QMouseEvent(
                event.type(), event.position(), event.globalPosition(),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, event.modifiers(),
            )
            super().mousePressEvent(fake)
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            fake = QMouseEvent(
                event.type(), event.position(), event.globalPosition(),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, event.modifiers(),
            )
            super().mouseReleaseEvent(fake)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._panning = False
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(factor, factor)
        else:
            self.scale(1 / factor, 1 / factor)

    def set_status_text(self, text: str):
        self._status_text = text
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect):
        super().drawForeground(painter, rect)
        if hasattr(self, '_status_text') and self._status_text:
            painter.save()
            # Draw in device (screen) coordinates so text is always the same size
            viewport = self.viewport()
            device_rect = viewport.rect()
            margin = 12
            font = painter.font()
            font.setPointSize(13)
            painter.setFont(font)
            painter.setPen(Qt.GlobalColor.white)
            metrics = painter.fontMetrics()
            text = self._status_text
            tw = metrics.horizontalAdvance(text)
            th = metrics.height()
            x = device_rect.right() - tw - margin
            y = device_rect.bottom() - margin
            painter.resetTransform()  # Ignore view transforms for overlay
            painter.drawText(x, y, text)
            painter.restore()
