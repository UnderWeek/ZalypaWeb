"""Compact Material bookmark bar used below the omnibox."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QWidget

from .material_theme import MaterialButton, MaterialIconButton


class BookmarksBar(QWidget):
    navigateRequested = Signal(str)
    manageRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("bookmarksBar")
        self.setProperty("materialRole", "surfaceContainer")
        self._items: list[dict[str, str]] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 3, 12, 5)
        self._layout.setSpacing(4)
        self._layout.addStretch(1)
        self.hide()

    def set_items(self, values: list[dict[str, str]]) -> None:
        self._items = list(values)
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        visible = self._items[:10]
        for bookmark in visible:
            title = bookmark.get("title") or bookmark.get("url") or "Закладка"
            button = MaterialButton(title[:28], self, variant="text")
            button.setToolTip(bookmark.get("url", ""))
            button.clicked.connect(
                lambda _checked=False, url=bookmark.get("url", ""): self.navigateRequested.emit(url)
            )
            self._layout.addWidget(button)
        if len(self._items) > len(visible):
            overflow = MaterialIconButton(self, variant="icon")
            overflow.setText("…")
            overflow.setToolTip("Другие закладки")
            overflow.clicked.connect(lambda: self._show_overflow(overflow))
            self._layout.addWidget(overflow)
        self._layout.addStretch(1)

    def _show_overflow(self, anchor: QWidget) -> None:
        menu = QMenu(self)
        for bookmark in self._items[10:]:
            action = menu.addAction(bookmark.get("title") or bookmark.get("url") or "Закладка")
            action.setToolTip(bookmark.get("url", ""))
            action.triggered.connect(
                lambda _checked=False, url=bookmark.get("url", ""): self.navigateRequested.emit(url)
            )
        menu.addSeparator()
        manage = menu.addAction("Управление закладками")
        manage.triggered.connect(self.manageRequested)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))


__all__ = ["BookmarksBar"]

