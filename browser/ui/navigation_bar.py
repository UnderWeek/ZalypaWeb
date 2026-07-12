"""Material navigation controls and the address/search field."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .material_theme import MaterialIconButton


class SuggestionModel(QAbstractListModel):
    """Small two-line completion model used by the omnibox."""

    TitleRole = Qt.ItemDataRole.UserRole + 1
    UrlRole = Qt.ItemDataRole.UserRole + 2
    KindRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, self.TitleRole):
            return item.get("title") or item.get("url", "")
        if role == self.UrlRole:
            return item.get("url", "")
        if role == self.KindRole:
            return item.get("kind", "history")
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.get("url", "")
        return None

    def set_items(self, items: Iterable[dict[str, str]]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item(self, row: int) -> dict[str, str] | None:
        return self._items[row] if 0 <= row < len(self._items) else None


class SuggestionDelegate(QStyledItemDelegate):
    """Paint suggestions as calm Material rows, independent of platform style."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        selected = bool(option.state & option.state.State_Selected)
        background = QColor("#EADDFF" if selected else "transparent")
        if selected:
            path = QPainterPath()
            path.addRoundedRect(option.rect.adjusted(5, 3, -5, -3), 12, 12)
            painter.fillPath(path, background)
        title = str(index.data(SuggestionModel.TitleRole) or "")
        url = str(index.data(SuggestionModel.UrlRole) or "")
        painter.setPen(QColor("#1D1B20"))
        title_font = option.font
        title_font.setWeight(600)
        painter.setFont(title_font)
        title_rect = option.rect.adjusted(18, 7, -12, -24)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QColor("#6750A4" if selected else "#49454F"))
        detail_font = option.font
        detail_font.setPointSizeF(max(8.0, detail_font.pointSizeF() - 1.0))
        detail_font.setWeight(400)
        painter.setFont(detail_font)
        url_rect = option.rect.adjusted(18, 27, -12, -4)
        metrics = painter.fontMetrics()
        painter.drawText(
            url_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(url, Qt.TextElideMode.ElideMiddle, url_rect.width()),
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):
        hint = super().sizeHint(option, index)
        hint.setHeight(58)
        return hint


class Omnibox(QLineEdit):
    """Address field with explicit activation and completion signals."""

    activated = Signal(str)
    queryChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("omnibox")
        self.setPlaceholderText("Введите адрес или поисковый запрос")
        self.setClearButtonEnabled(True)
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model = SuggestionModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(8)
        popup = self._completer.popup()
        popup.setObjectName("omniboxSuggestions")
        popup.setItemDelegate(SuggestionDelegate(popup))
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCompleter(self._completer)
        self.returnPressed.connect(lambda: self.activated.emit(self.text().strip()))
        self.textEdited.connect(self._on_text_edited)
        self._completer.activated[QModelIndex].connect(self._completion_activated)

    def set_suggestions(self, items: Iterable[dict[str, str]]) -> None:
        self._model.set_items(items)
        if self.hasFocus() and self.text().strip() and self._model.rowCount() > 0:
            popup = self._completer.popup()
            popup.setMinimumWidth(max(420, self.width()))
            self._completer.complete()

    def focus_and_select(self) -> None:
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.selectAll()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_text_edited(self, text: str) -> None:
        self.queryChanged.emit(text.strip())

    def _completion_activated(self, index: QModelIndex) -> None:
        item = self._model.item(index.row())
        if item:
            destination = item.get("url") or item.get("title", "")
            self.setText(destination)
            self.activated.emit(destination)


class SecurityIndicator(QWidget):
    """Compact HTTPS/security indicator rendered without native controls."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._secure = False
        self._internal = True
        self._warning = False
        self.setToolTip("Внутренняя страница Auralis")

    def set_state(self, *, secure: bool, internal: bool = False, warning: bool = False) -> None:
        self._secure, self._internal, self._warning = secure, internal, warning
        if internal:
            self.setToolTip("Защищённая внутренняя страница Auralis")
        elif warning:
            self.setToolTip("Соединение требует внимания")
        elif secure:
            self.setToolTip("Защищённое HTTPS-соединение")
        else:
            self.setToolTip("Незащищённое соединение")
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._warning:
            color = QColor("#BA1A1A")
        elif self._secure or self._internal:
            color = QColor("#386A20")
        else:
            color = QColor("#79747E")
        painter.setPen(
            QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        )
        if self._internal:
            painter.drawEllipse(QPoint(17, 17), 8, 8)
            painter.drawEllipse(QPoint(17, 17), 3, 3)
            return
        body = QRect(10, 15, 14, 11)
        path = QPainterPath()
        path.addRoundedRect(body, 3, 3)
        painter.drawPath(path)
        if self._secure:
            painter.drawArc(QRect(12, 7, 10, 15), 0, 180 * 16)
        else:
            painter.drawArc(QRect(16, 7, 10, 15), 10 * 16, 145 * 16)


class NavigationBar(QWidget):
    """Back/forward controls, omnibox and primary page actions."""

    backRequested = Signal()
    forwardRequested = Signal()
    reloadRequested = Signal()
    stopRequested = Signal()
    homeRequested = Signal()
    bookmarkRequested = Signal()
    menuRequested = Signal(QPoint)
    siteInfoRequested = Signal()
    navigateRequested = Signal(str)
    suggestionQueryChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navigationBar")
        self.setProperty("materialRole", "surfaceContainer")
        self._loading = False

        self.back_button = self._button("←", "Назад (Alt+←)")
        self.forward_button = self._button("→", "Вперёд (Alt+→)")
        self.reload_button = self._button("↻", "Обновить (Ctrl+R)")
        self.home_button = self._button("⌂", "Домашняя страница")
        self.security = SecurityIndicator(self)
        self.omnibox = Omnibox(self)
        self.bookmark_button = self._button("☆", "Добавить закладку (Ctrl+D)")
        self.menu_button = self._button("⋮", "Меню Auralis")
        self.progress = QProgressBar(self)
        self.progress.setObjectName("pageLoadProgress")
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.hide()

        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 7)
        row.setSpacing(4)
        for button in (self.back_button, self.forward_button, self.reload_button, self.home_button):
            row.addWidget(button)
        row.addSpacing(2)
        row.addWidget(self.security)
        row.addWidget(self.omnibox, 1)
        row.addWidget(self.bookmark_button)
        row.addWidget(self.menu_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(row)
        layout.addWidget(self.progress)

        self.back_button.clicked.connect(self.backRequested)
        self.forward_button.clicked.connect(self.forwardRequested)
        self.reload_button.clicked.connect(self._reload_or_stop)
        self.home_button.clicked.connect(self.homeRequested)
        self.bookmark_button.clicked.connect(self.bookmarkRequested)
        self.menu_button.clicked.connect(
            lambda: self.menuRequested.emit(
                self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft())
            )
        )
        self.security.clicked.connect(self.siteInfoRequested)
        self.omnibox.activated.connect(self.navigateRequested)
        self.omnibox.queryChanged.connect(self.suggestionQueryChanged)

    @staticmethod
    def _button(text: str, tooltip: str) -> MaterialIconButton:
        button = MaterialIconButton(variant="icon")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip.split(" (")[0])
        return button

    def set_url(self, url: str, *, force: bool = False) -> None:
        if force or not self.omnibox.hasFocus():
            shown = "" if url.startswith("auralis://newtab") else url
            self.omnibox.setText(shown)
            self.omnibox.setCursorPosition(0)
        internal = url.startswith("auralis://")
        secure = url.lower().startswith("https://")
        self.security.set_state(
            secure=secure, internal=internal, warning=bool(url) and not secure and not internal
        )

    def set_navigation_state(self, *, can_back: bool, can_forward: bool) -> None:
        self.back_button.setEnabled(can_back)
        self.forward_button.setEnabled(can_forward)

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        self.reload_button.setText("×" if loading else "↻")
        self.reload_button.setToolTip("Остановить загрузку (Esc)" if loading else "Обновить (Ctrl+R)")
        if not loading:
            self.progress.hide()

    def set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, value)))
        self.progress.setVisible(self._loading and value < 100)

    def set_bookmarked(self, bookmarked: bool) -> None:
        self.bookmark_button.setText("★" if bookmarked else "☆")
        self.bookmark_button.setProperty("bookmarked", bookmarked)
        self.bookmark_button.style().unpolish(self.bookmark_button)
        self.bookmark_button.style().polish(self.bookmark_button)

    def _reload_or_stop(self) -> None:
        if self._loading:
            self.stopRequested.emit()
        else:
            self.reloadRequested.emit()
