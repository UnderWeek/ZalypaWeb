"""Reusable Material dialogs and popovers for Auralis Browser."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from urllib.parse import urlparse

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .material_theme import MaterialButton, MaterialCard, set_elevation


class MaterialDialog(QDialog):
    """Base dialog with a consistent header, body and action row."""

    def __init__(
        self,
        title: str,
        description: str = "",
        parent: QWidget | None = None,
        *,
        width: int = 520,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(26, 24, 26, 20)
        self._root.setSpacing(12)
        heading = QLabel(title, self)
        heading.setProperty("materialRole", "headline")
        self._root.addWidget(heading)
        if description:
            subtitle = QLabel(description, self)
            subtitle.setProperty("materialRole", "body")
            subtitle.setWordWrap(True)
            self._root.addWidget(subtitle)
        self.body = QWidget(self)
        self.body.setProperty("materialRole", "transparent")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 8, 0, 8)
        self.body_layout.setSpacing(12)
        self._root.addWidget(self.body, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addStretch(1)
        self._root.addLayout(self.actions)

    def add_action(self, text: str, *, variant: str = "text", accept: bool = False) -> MaterialButton:
        button = MaterialButton(text, self, variant=variant)
        button.clicked.connect(self.accept if accept else self.reject)
        self.actions.addWidget(button)
        return button


class BookmarkDialog(MaterialDialog):
    """Create or edit a bookmark without depending on the database model."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "",
        url: str = "",
        folders: list[tuple[object, str]] | None = None,
        folder_id: object | None = None,
    ) -> None:
        super().__init__("Сохранить закладку", "Добавьте страницу в коллекцию Auralis.", parent)
        self.title_edit = QLineEdit(title, self.body)
        self.title_edit.setPlaceholderText("Название")
        self.url_edit = QLineEdit(url, self.body)
        self.url_edit.setPlaceholderText("https://example.com")
        self.folder_combo = QComboBox(self.body)
        self.folder_combo.addItem("Без папки", None)
        for identifier, name in folders or []:
            self.folder_combo.addItem(name, identifier)
        if folder_id is not None:
            index = self.folder_combo.findData(folder_id)
            if index >= 0:
                self.folder_combo.setCurrentIndex(index)
        self.error_label = QLabel(self.body)
        self.error_label.setProperty("materialRole", "error")
        self.error_label.hide()
        self.body_layout.addWidget(self._label("Название"))
        self.body_layout.addWidget(self.title_edit)
        self.body_layout.addWidget(self._label("Адрес"))
        self.body_layout.addWidget(self.url_edit)
        self.body_layout.addWidget(self._label("Папка"))
        self.body_layout.addWidget(self.folder_combo)
        self.body_layout.addWidget(self.error_label)
        self.add_action("Отмена")
        save = MaterialButton("Сохранить", self, variant="filled")
        save.clicked.connect(self.accept)
        self.actions.addWidget(save)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("materialRole", "label")
        return label

    def bookmark_data(self) -> dict[str, object]:
        return {
            "title": self.title_edit.text().strip(),
            "url": self.url_edit.text().strip(),
            "folder_id": self.folder_combo.currentData(),
        }

    def accept(self) -> None:
        data = self.bookmark_data()
        if not data["title"]:
            self._show_error("Укажите название закладки.")
            self.title_edit.setFocus()
            return
        url = str(data["url"])
        candidate = url if "://" in url else f"https://{url}"
        parsed = urlparse(candidate)
        if not parsed.hostname:
            self._show_error("Введите корректный адрес сайта.")
            self.url_edit.setFocus()
            return
        if "://" not in url:
            self.url_edit.setText(candidate)
        super().accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()


class QuickLinkDialog(MaterialDialog):
    """Editor for a native start-page quick link."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "",
        url: str = "",
    ) -> None:
        super().__init__("Быстрый доступ", "Сайт появится на стартовой странице.", parent, width=480)
        self.title_edit = QLineEdit(title, self.body)
        self.title_edit.setPlaceholderText("Название")
        self.url_edit = QLineEdit(url, self.body)
        self.url_edit.setPlaceholderText("Адрес сайта")
        self.error_label = QLabel(self.body)
        self.error_label.setProperty("materialRole", "error")
        self.error_label.hide()
        self.body_layout.addWidget(self.title_edit)
        self.body_layout.addWidget(self.url_edit)
        self.body_layout.addWidget(self.error_label)
        self.add_action("Отмена")
        save = MaterialButton("Готово", self, variant="filled")
        save.clicked.connect(self.accept)
        self.actions.addWidget(save)

    def link_data(self) -> dict[str, str]:
        return {"title": self.title_edit.text().strip(), "url": self.url_edit.text().strip()}

    def accept(self) -> None:
        data = self.link_data()
        if not data["title"] or not data["url"]:
            self.error_label.setText("Заполните название и адрес.")
            self.error_label.show()
            return
        candidate = data["url"] if "://" in data["url"] else f"https://{data['url']}"
        if not urlparse(candidate).hostname:
            self.error_label.setText("Не удалось распознать адрес сайта.")
            self.error_label.show()
            return
        self.url_edit.setText(candidate)
        super().accept()


class PermissionDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    BLOCK = "block"


class PermissionDialog(MaterialDialog):
    """Site permission prompt with explicit one-time/permanent decisions."""

    decisionMade = Signal(str, str, object)

    PERMISSION_LABELS = {
        "camera": "Камера",
        "microphone": "Микрофон",
        "geolocation": "Местоположение",
        "notifications": "Уведомления",
        "clipboard": "Буфер обмена",
        "midi": "MIDI-устройства",
        "desktop_video_capture": "Захват экрана",
    }

    def __init__(
        self,
        origin: str,
        permission: str,
        parent: QWidget | None = None,
    ) -> None:
        label = self.PERMISSION_LABELS.get(permission, permission.replace("_", " ").capitalize())
        super().__init__("Разрешение сайта", f"{origin} запрашивает: {label}", parent, width=500)
        self.origin = origin
        self.permission = permission
        self.decision: PermissionDecision | None = None
        notice = MaterialCard(self.body, role="surfaceContainer")
        text = QLabel(
            "Разрешайте доступ только сайтам, которым доверяете. Решение всегда можно изменить в настройках.",
            notice,
        )
        text.setWordWrap(True)
        text.setProperty("materialRole", "body")
        layout = QVBoxLayout(notice)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addWidget(text)
        self.body_layout.addWidget(notice)
        block = MaterialButton("Блокировать", self, variant="text")
        once = MaterialButton("Разрешить сейчас", self, variant="tonal")
        always = MaterialButton("Разрешать всегда", self, variant="filled")
        block.clicked.connect(lambda: self._finish(PermissionDecision.BLOCK))
        once.clicked.connect(lambda: self._finish(PermissionDecision.ALLOW_ONCE))
        always.clicked.connect(lambda: self._finish(PermissionDecision.ALLOW_ALWAYS))
        self.actions.addWidget(block)
        self.actions.addWidget(once)
        self.actions.addWidget(always)

    def _finish(self, decision: PermissionDecision) -> None:
        self.decision = decision
        self.decisionMade.emit(self.origin, self.permission, decision)
        self.done(QDialog.DialogCode.Accepted if decision is not PermissionDecision.BLOCK else QDialog.DialogCode.Rejected)


@dataclass(frozen=True, slots=True)
class ClearDataSelection:
    time_range: str
    history: bool
    downloads: bool
    cookies: bool
    cache: bool
    permissions: bool


class ClearBrowsingDataDialog(MaterialDialog):
    """Collects a precise destructive-data selection for the controller."""

    clearRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Очистить данные браузера",
            "Выберите период и типы локальных данных. Закладки и пароли не затрагиваются.",
            parent,
            width=560,
        )
        self.time_range = QComboBox(self.body)
        for label, value in (
            ("Последний час", "hour"),
            ("Последние 24 часа", "day"),
            ("Последние 7 дней", "week"),
            ("Последние 4 недели", "month"),
            ("За всё время", "all"),
        ):
            self.time_range.addItem(label, value)
        self.history = QCheckBox("История посещений", self.body)
        self.downloads = QCheckBox("История загрузок", self.body)
        self.cookies = QCheckBox("Cookies и данные сайтов", self.body)
        self.cache = QCheckBox("Изображения и файлы в кеше", self.body)
        self.permissions = QCheckBox("Разрешения сайтов", self.body)
        self.history.setChecked(True)
        self.cookies.setChecked(True)
        self.cache.setChecked(True)
        self.error_label = QLabel("Выберите хотя бы один тип данных.", self.body)
        self.error_label.setProperty("materialRole", "error")
        self.error_label.hide()
        self.body_layout.addWidget(self.time_range)
        card = MaterialCard(self.body, role="surfaceContainer")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        for control in (self.history, self.downloads, self.cookies, self.cache, self.permissions):
            card_layout.addWidget(control)
        self.body_layout.addWidget(card)
        self.body_layout.addWidget(self.error_label)
        self.add_action("Отмена")
        clear = MaterialButton("Очистить", self, variant="danger")
        clear.clicked.connect(self.accept)
        self.actions.addWidget(clear)

    def selection(self) -> ClearDataSelection:
        return ClearDataSelection(
            time_range=str(self.time_range.currentData()),
            history=self.history.isChecked(),
            downloads=self.downloads.isChecked(),
            cookies=self.cookies.isChecked(),
            cache=self.cache.isChecked(),
            permissions=self.permissions.isChecked(),
        )

    def accept(self) -> None:
        result = self.selection()
        choices = asdict(result)
        choices.pop("time_range", None)
        if not any(choices.values()):
            self.error_label.show()
            return
        self.clearRequested.emit(result)
        super().accept()


@dataclass(frozen=True, slots=True)
class SiteInformation:
    origin: str
    secure: bool = False
    certificate_issuer: str = ""
    certificate_expires: str = ""
    cookies_count: int = 0
    permissions_count: int = 0


class SiteInformationDialog(MaterialDialog):
    managePermissionsRequested = Signal(str)
    clearSiteDataRequested = Signal(str)

    def __init__(self, information: SiteInformation | dict[str, object], parent: QWidget | None = None) -> None:
        if isinstance(information, dict):
            information = SiteInformation(**information)
        self.information = information
        status = "Соединение защищено" if information.secure else "Соединение не защищено"
        super().__init__("Информация о сайте", information.origin, parent, width=540)
        security = MaterialCard(self.body, role="primaryContainer" if information.secure else "surfaceContainer")
        status_label = QLabel(("✓  " if information.secure else "!  ") + status, security)
        status_label.setProperty("materialRole", "subtitle")
        security_layout = QVBoxLayout(security)
        security_layout.setContentsMargins(16, 14, 16, 14)
        security_layout.addWidget(status_label)
        if information.certificate_issuer:
            certificate = QLabel(
                f"Сертификат: {information.certificate_issuer}\nДействителен до: {information.certificate_expires}",
                security,
            )
            certificate.setProperty("materialRole", "body")
            security_layout.addWidget(certificate)
        self.body_layout.addWidget(security)
        stats = QLabel(
            f"Cookies: {information.cookies_count}    ·    Особые разрешения: {information.permissions_count}",
            self.body,
        )
        stats.setProperty("materialRole", "body")
        self.body_layout.addWidget(stats)
        clear = MaterialButton("Удалить данные сайта", self, variant="outlined")
        permissions = MaterialButton("Разрешения", self, variant="tonal")
        done = MaterialButton("Готово", self, variant="filled")
        clear.clicked.connect(lambda: self.clearSiteDataRequested.emit(information.origin))
        permissions.clicked.connect(lambda: self.managePermissionsRequested.emit(information.origin))
        done.clicked.connect(self.accept)
        self.actions.addWidget(clear)
        self.actions.addWidget(permissions)
        self.actions.addWidget(done)


class TabPreviewPopup(MaterialCard):
    """Non-activating popup displayed after hovering a browser tab."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, role="surfaceContainer", elevation=3)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(320)
        self._preview = QLabel(self)
        self._preview.setFixedSize(292, 164)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setProperty("materialRole", "surface")
        self._title = QLabel(self)
        self._title.setProperty("materialRole", "subtitle")
        self._url = QLabel(self)
        self._url.setProperty("materialRole", "label")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(5)
        layout.addWidget(self._preview)
        layout.addWidget(self._title)
        layout.addWidget(self._url)

    def show_preview(self, title: str, url: str, pixmap: QPixmap | None, anchor: QPoint) -> None:
        self._title.setText(title)
        self._url.setText(url)
        if pixmap and not pixmap.isNull():
            self._preview.setPixmap(
                pixmap.scaled(
                    self._preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._preview.setText("Предпросмотр страницы")
            self._preview.setPixmap(QPixmap())
        self.adjustSize()
        self.move(anchor + QPoint(0, 8))
        self.show()


__all__ = [
    "BookmarkDialog",
    "ClearBrowsingDataDialog",
    "ClearDataSelection",
    "MaterialDialog",
    "PermissionDecision",
    "PermissionDialog",
    "QuickLinkDialog",
    "SiteInformation",
    "SiteInformationDialog",
    "TabPreviewPopup",
]
