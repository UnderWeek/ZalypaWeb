"""Lightweight Material overlays shared by the browser window."""

from __future__ import annotations

import contextlib

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Snackbar(QFrame):
    """Transient bottom message with an optional action."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("snackbar")
        self.setProperty("materialRole", "inverseSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._label = QLabel(self)
        self._label.setObjectName("snackbarLabel")
        self._action = QPushButton(self)
        self._action.setObjectName("snackbarAction")
        self._action.hide()
        self._action_callback = None
        self._action.clicked.connect(self._invoke_action)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._action)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_animated)
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._animation = QPropertyAnimation(self._opacity, b"opacity", self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hide()

    def show_message(self, text: str, *, action_text: str = "", callback=None, timeout: int = 4200) -> None:
        self._label.setText(text)
        if action_text and callback is not None:
            self._action.setText(action_text)
            self._action_callback = callback
            self._action.show()
        else:
            self._action_callback = None
            self._action.hide()
        self.adjustSize()
        self.setMinimumWidth(min(540, max(290, self.sizeHint().width())))
        self._reposition()
        self.show()
        self.raise_()
        self._animation.stop()
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()
        self._timer.start(timeout)

    def _invoke_action(self) -> None:
        callback = self._action_callback
        self._action_callback = None
        if callback is not None:
            callback()
        self.hide_animated()

    def hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._animation.stop()
        self._animation.setStartValue(self._opacity.opacity())
        self._animation.setEndValue(0.0)
        self._animation.finished.connect(self._finish_hide)
        self._animation.start()

    def _finish_hide(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._animation.finished.disconnect(self._finish_hide)
        self.hide()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(self.minimumWidth(), max(240, parent.width() - 40))
        height = max(52, self.sizeHint().height())
        self.setGeometry(20, parent.height() - height - 22, width, height)

    def parent_resized(self) -> None:
        if self.isVisible():
            self._reposition()


class TabPreview(QFrame):
    """Hover preview for a background tab."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("tabPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._image = QLabel(self)
        self._image.setObjectName("tabPreviewImage")
        self._image.setFixedSize(310, 175)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel(self)
        self._title.setObjectName("tabPreviewTitle")
        self._title.setWordWrap(False)
        self._url = QLabel(self)
        self._url.setObjectName("tabPreviewUrl")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(7)
        layout.addWidget(self._image)
        layout.addWidget(self._title)
        layout.addWidget(self._url)
        self.setFixedWidth(330)

    def show_preview(self, pixmap: QPixmap, title: str, url: str, anchor: QPoint) -> None:
        if not pixmap.isNull():
            self._image.setPixmap(
                pixmap.scaled(
                    self._image.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._image.setText("Предпросмотр появится после загрузки")
        metrics = self._title.fontMetrics()
        self._title.setText(metrics.elidedText(title or "Новая вкладка", Qt.TextElideMode.ElideRight, 300))
        self._url.setText(self._url.fontMetrics().elidedText(url, Qt.TextElideMode.ElideMiddle, 300))
        self.adjustSize()
        self.move(anchor + QPoint(0, 8))
        self.show()
        self.raise_()
