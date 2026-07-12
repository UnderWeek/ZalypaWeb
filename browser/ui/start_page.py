"""Native polished start page for Auralis Browser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QContextMenuEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from .material_theme import MaterialCard, MaterialIconButton, set_elevation


@dataclass(frozen=True, slots=True)
class QuickLink:
    """Serializable quick-link model."""

    link_id: str
    title: str
    url: str
    icon_path: str | None = None
    color: str | None = None

    @classmethod
    def from_value(cls, value: QuickLink | dict[str, object]) -> QuickLink:
        if isinstance(value, cls):
            return value
        return cls(
            link_id=str(value.get("link_id") or value.get("id") or value.get("url") or ""),
            title=str(value.get("title") or value.get("name") or "Сайт"),
            url=str(value.get("url") or ""),
            icon_path=str(value["icon_path"]) if value.get("icon_path") else None,
            color=str(value["color"]) if value.get("color") else None,
        )


class _SearchField(QLineEdit):
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("startPageSearch")
        self.setProperty("materialRole", "search")
        self.setPlaceholderText("Найдите что-нибудь или введите адрес")
        self.setClearButtonEnabled(True)
        self.setMinimumWidth(320)
        self.setMaximumWidth(760)
        self.setAccessibleName("Поиск и адрес")
        self.returnPressed.connect(self._submit)

    def _submit(self) -> None:
        value = self.text().strip()
        if value:
            self.submitted.emit(value)


class QuickLinkCard(MaterialCard):
    activated = Signal(str)
    editRequested = Signal(str)
    removeRequested = Signal(str)

    def __init__(self, link: QuickLink, parent: QWidget | None = None) -> None:
        super().__init__(parent, role="surfaceContainer", elevation=0)
        self.link = link
        self.setObjectName("quickLinkCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(QSize(148, 112))
        self.setToolTip(link.url)
        self.setAccessibleName(f"{link.title}, {link.url}")

        favicon = QLabel(self)
        favicon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        favicon.setFixedSize(42, 42)
        favicon.setProperty("materialRole", "primaryContainer")
        icon = QIcon(link.icon_path) if link.icon_path else QIcon()
        if not icon.isNull():
            favicon.setPixmap(icon.pixmap(26, 26))
        else:
            favicon.setText(self._monogram(link))
            favicon.setStyleSheet("font-size: 15pt; font-weight: 700; border-radius: 15px;")

        title = QLabel(link.title, self)
        title.setProperty("materialRole", "subtitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(False)
        title.setToolTip(link.title)

        host = QLabel(self._display_host(link.url), self)
        host.setProperty("materialRole", "label")
        host.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(3)
        layout.addWidget(favicon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(3)
        layout.addWidget(title)
        layout.addWidget(host)

    @staticmethod
    def _monogram(link: QuickLink) -> str:
        source = link.title.strip() or QuickLinkCard._display_host(link.url)
        return source[:1].upper() if source else "•"

    @staticmethod
    def _display_host(url: str) -> str:
        candidate = url if "://" in url else f"https://{url}"
        host = urlparse(candidate).hostname or url
        return host.removeprefix("www.")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.link.url)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.link.url)
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        menu = QMenu(self)
        open_action = menu.addAction("Открыть")
        edit_action = menu.addAction("Изменить")
        remove_action = menu.addAction("Удалить")
        chosen = menu.exec(event.globalPos())
        if chosen is open_action:
            self.activated.emit(self.link.url)
        elif chosen is edit_action:
            self.editRequested.emit(self.link.link_id)
        elif chosen is remove_action:
            self.removeRequested.emit(self.link.link_id)

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        set_elevation(self, 2)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self.setGraphicsEffect(None)
        super().leaveEvent(event)


class _AddLinkCard(MaterialCard):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, role="surface")
        self.setFixedSize(QSize(148, 112))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Добавить быстрый доступ")
        plus = QLabel("+", self)
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setProperty("materialRole", "headline")
        text = QLabel("Добавить", self)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setProperty("materialRole", "label")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.addWidget(plus, 1)
        layout.addWidget(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class StartPage(QWidget):
    """Auralis start page with search, greeting and editable quick links."""

    navigateRequested = Signal(str)
    searchRequested = Signal(str)
    quickLinkAddRequested = Signal()
    quickLinkEditRequested = Signal(str)
    quickLinkRemoveRequested = Signal(str)
    customizeRequested = Signal()

    _URL_PATTERN = re.compile(
        r"^(?:[a-z][a-z0-9+.-]*://|localhost(?::\d+)?(?:/|$)|(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:/|$))",
        re.IGNORECASE,
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("auralisStartPage")
        self._user_name = ""
        self._links: list[QuickLink] = []
        self._columns = 0

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget(self._scroll)
        self._content.setProperty("materialRole", "transparent")
        self._scroll.setWidget(self._content)

        self._greeting = QLabel(self._content)
        self._greeting.setProperty("materialRole", "display")
        self._greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle = QLabel("Спокойное пространство для любопытных мыслей", self._content)
        self._subtitle.setProperty("materialRole", "body")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._search_card = MaterialCard(self._content, role="surfaceContainer", elevation=2)
        search_icon = QLabel("⌕", self._search_card)
        search_icon.setProperty("materialRole", "title")
        search_icon.setAccessibleName("Поиск")
        self._search = _SearchField(self._search_card)
        self._voice_button = MaterialIconButton(self._search_card)
        self._voice_button.setText("🎙")
        self._voice_button.setToolTip("Голосовой поиск (будет доступен после подключения сервиса)")
        self._voice_button.setEnabled(False)
        search_layout = QHBoxLayout(self._search_card)
        search_layout.setContentsMargins(14, 3, 8, 3)
        search_layout.setSpacing(4)
        search_layout.addWidget(search_icon)
        search_layout.addWidget(self._search, 1)
        search_layout.addWidget(self._voice_button)
        self._search_card.setMaximumWidth(820)
        self._search_card.setMinimumWidth(360)

        quick_header = QWidget(self._content)
        quick_header.setProperty("materialRole", "transparent")
        quick_title = QLabel("Быстрый доступ", quick_header)
        quick_title.setProperty("materialRole", "title")
        customize = MaterialIconButton(quick_header)
        customize.setText("⋮")
        customize.setToolTip("Настроить стартовую страницу")
        customize.clicked.connect(self.customizeRequested)
        header_layout = QHBoxLayout(quick_header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(quick_title)
        header_layout.addStretch(1)
        header_layout.addWidget(customize)

        self._links_host = QWidget(self._content)
        self._links_host.setProperty("materialRole", "transparent")
        self._links_grid = QGridLayout(self._links_host)
        self._links_grid.setContentsMargins(0, 0, 0, 0)
        self._links_grid.setHorizontalSpacing(14)
        self._links_grid.setVerticalSpacing(14)
        self._links_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._privacy_chip = MaterialCard(self._content, role="surfaceContainer")
        chip_icon = QLabel("◈", self._privacy_chip)
        chip_text = QLabel("Защита Auralis активна", self._privacy_chip)
        chip_text.setProperty("materialRole", "label")
        chip_layout = QHBoxLayout(self._privacy_chip)
        chip_layout.setContentsMargins(12, 5, 12, 5)
        chip_layout.setSpacing(7)
        chip_layout.addWidget(chip_icon)
        chip_layout.addWidget(chip_text)
        self._privacy_chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(32, 54, 32, 32)
        content_layout.setSpacing(12)
        content_layout.addSpacerItem(
            QSpacerItem(1, 16, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        )
        content_layout.addWidget(self._greeting)
        content_layout.addWidget(self._subtitle)
        content_layout.addSpacing(22)
        content_layout.addWidget(self._search_card, 0, Qt.AlignmentFlag.AlignHCenter)
        content_layout.addSpacing(34)
        content_layout.addWidget(quick_header)
        content_layout.addWidget(self._links_host)
        content_layout.addSpacing(26)
        content_layout.addWidget(self._privacy_chip, 0, Qt.AlignmentFlag.AlignHCenter)
        content_layout.addSpacerItem(
            QSpacerItem(1, 16, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)

        self._search.submitted.connect(self._handle_submission)
        shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        shortcut.activated.connect(self.focus_search)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(60_000)
        self._clock_timer.timeout.connect(self._update_greeting)
        self._clock_timer.start()
        self._update_greeting()
        self.set_quick_links([])

    def set_user_name(self, name: str | None) -> None:
        self._user_name = (name or "").strip()
        self._update_greeting()

    def set_quick_links(self, links: list[QuickLink | dict[str, object]]) -> None:
        self._links = [QuickLink.from_value(item) for item in links if item]
        self._rebuild_links(force=True)

    def quick_links(self) -> tuple[QuickLink, ...]:
        return tuple(self._links)

    def focus_search(self) -> None:
        self._search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._search.selectAll()

    def set_search_text(self, text: str) -> None:
        self._search.setText(text)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._rebuild_links()

    def _rebuild_links(self, *, force: bool = False) -> None:
        available = max(320, self._scroll.viewport().width() - 64)
        columns = max(2, min(6, available // 162))
        if not force and columns == self._columns:
            return
        self._columns = columns
        while self._links_grid.count():
            item = self._links_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cards: list[QWidget] = []
        for link in self._links:
            card = QuickLinkCard(link, self._links_host)
            card.activated.connect(self.navigateRequested)
            card.editRequested.connect(self.quickLinkEditRequested)
            card.removeRequested.connect(self.quickLinkRemoveRequested)
            cards.append(card)
        add_card = _AddLinkCard(self._links_host)
        add_card.clicked.connect(self.quickLinkAddRequested)
        cards.append(add_card)
        for index, card in enumerate(cards):
            self._links_grid.addWidget(card, index // columns, index % columns)

    def _handle_submission(self, value: str) -> None:
        if self._URL_PATTERN.match(value):
            url = value if "://" in value else f"https://{value}"
            self.navigateRequested.emit(url)
        else:
            self.searchRequested.emit(value)

    def _update_greeting(self) -> None:
        hour = datetime.now().hour
        if 5 <= hour < 12:
            base = "Доброе утро"
        elif 12 <= hour < 18:
            base = "Добрый день"
        elif 18 <= hour < 23:
            base = "Добрый вечер"
        else:
            base = "Доброй ночи"
        suffix = f", {self._user_name}" if self._user_name else ""
        self._greeting.setText(f"{base}{suffix}")


__all__ = ["QuickLink", "QuickLinkCard", "StartPage"]
