"""Material settings experience for Auralis Browser."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QColorDialog,
)

from .material_theme import DEFAULT_ACCENT, MaterialButton, MaterialCard, MaterialIconButton


DEFAULT_SETTINGS: dict[str, object] = {
    "general.home_page": "auralis://start",
    "general.search_engine": "google",
    "general.language": "ru",
    "general.restore_session": True,
    "general.open_external_links_new_tab": True,
    "privacy.tracking_protection": "standard",
    "privacy.third_party_cookies": True,
    "privacy.do_not_track": False,
    "privacy.safe_browsing": True,
    "privacy.https_only": False,
    "appearance.theme": "system",
    "appearance.accent": DEFAULT_ACCENT,
    "appearance.density": "comfortable",
    "appearance.ui_scale": 100,
    "appearance.show_bookmarks_bar": False,
    "performance.hardware_acceleration": True,
    "performance.memory_saver": True,
    "performance.preload_pages": True,
    "downloads.directory": "",
    "downloads.ask_location": False,
    "downloads.notifications": True,
    "sync.enabled": False,
}


class MaterialSwitch(QAbstractButton):
    """Animated, dependency-free Material 3 switch."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Переключатель")
        self._offset = 0.0
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(52, 32)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = max(0.0, min(1.0, value))
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        if not self._animation.state() == QPropertyAnimation.State.Running:
            self._set_offset(1.0 if checked else 0.0)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(1, 3, self.width() - 2, self.height() - 6)
        palette = self.palette()
        if self.isChecked():
            track = palette.color(palette.ColorRole.Highlight)
            thumb = palette.color(palette.ColorRole.HighlightedText)
        else:
            track = palette.color(palette.ColorRole.Mid)
            thumb = palette.color(palette.ColorRole.Base)
        if not self.isEnabled():
            track.setAlpha(75)
            thumb.setAlpha(120)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(bounds, bounds.height() / 2, bounds.height() / 2)
        diameter = 22.0 if self.isChecked() else 18.0
        travel = bounds.width() - diameter - 8.0
        x = bounds.left() + 4.0 + travel * self._offset
        y = bounds.center().y() - diameter / 2
        painter.setBrush(thumb)
        painter.drawEllipse(QRectF(QPointF(x, y), QSize(int(diameter), int(diameter))))


class AccentButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, color: str = DEFAULT_ACCENT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Выбрать динамический цвет")
        self.setAccessibleName("Цвет интерфейса")
        self._color = QColor(color) if QColor(color).isValid() else QColor(DEFAULT_ACCENT)
        self.setFixedSize(52, 36)
        self.clicked.connect(self._choose)
        self._refresh()

    @property
    def color(self) -> str:
        return self._color.name(QColor.NameFormat.HexRgb).upper()

    def set_color(self, color: str) -> None:
        value = QColor(color)
        if not value.isValid():
            return
        self._color = value
        self._refresh()

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"QPushButton {{ background: {self.color}; border: 4px solid rgba(255,255,255,90); "
            "border-radius: 14px; min-height: 28px; padding: 0; }}"
        )

    def _choose(self) -> None:
        value = QColorDialog.getColor(self._color, self, "Цвет Material You")
        if value.isValid() and value != self._color:
            self._color = value
            self._refresh()
            self.colorChanged.emit(self.color)


class SettingRow(QWidget):
    """A labelled setting with a trailing control."""

    def __init__(
        self,
        title: str,
        description: str,
        control: QWidget,
        parent: QWidget | None = None,
        *,
        keywords: str = "",
    ) -> None:
        super().__init__(parent)
        self.title = title
        self.description = description
        self.keywords = f"{title} {description} {keywords}".casefold()
        self.setProperty("materialRole", "transparent")
        title_label = QLabel(title, self)
        title_label.setProperty("materialRole", "subtitle")
        description_label = QLabel(description, self)
        description_label.setProperty("materialRole", "body")
        description_label.setWordWrap(True)
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(title_label)
        text_layout.addWidget(description_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(24)
        layout.addLayout(text_layout, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self.control = control


class SettingsSection(QWidget):
    """Scrollable page containing grouped settings cards."""

    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[SettingRow] = []
        self._content = QWidget(self)
        self._content.setProperty("materialRole", "transparent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(30, 28, 34, 36)
        self._layout.setSpacing(16)
        heading = QLabel(title, self._content)
        heading.setProperty("materialRole", "headline")
        subtitle = QLabel(description, self._content)
        subtitle.setProperty("materialRole", "body")
        subtitle.setWordWrap(True)
        self._layout.addWidget(heading)
        self._layout.addWidget(subtitle)
        self._layout.addSpacing(4)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)

    def add_group(self, title: str, rows: list[SettingRow]) -> MaterialCard:
        label = QLabel(title, self._content)
        label.setProperty("materialRole", "label")
        self._layout.addWidget(label)
        card = MaterialCard(self._content, role="surfaceContainer")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 2, 0, 2)
        card_layout.setSpacing(0)
        for number, row in enumerate(rows):
            card_layout.addWidget(row)
            self.rows.append(row)
            if number != len(rows) - 1:
                divider = QFrame(card)
                divider.setFrameShape(QFrame.Shape.HLine)
                divider.setProperty("materialRole", "surface")
                divider.setFixedHeight(1)
                card_layout.addWidget(divider)
        self._layout.addWidget(card)
        return card

    def finish(self) -> None:
        self._layout.addStretch(1)


@dataclass(slots=True)
class _Binding:
    widget: QWidget
    read: Callable[[], object]
    write: Callable[[object], None]


class SettingsPanel(QWidget):
    """Embeddable complete settings UI.

    All persistent values use dotted keys and are exposed through
    :meth:`settings`.  Side effects (cache clearing, permissions, profiles) are
    requests only; their implementation belongs to the application controller.
    """

    settingChanged = Signal(str, object)
    themeModeChanged = Signal(str)
    accentChanged = Signal(str)
    densityChanged = Signal(str)
    clearBrowsingDataRequested = Signal()
    clearCacheRequested = Signal()
    managePermissionsRequested = Signal()
    extensionsRequested = Signal()
    profilesRequested = Signal()
    syncRequested = Signal()
    defaultBrowserRequested = Signal()
    openDownloadsFolderRequested = Signal()

    SECTIONS = (
        ("general", "⌂", "Общие"),
        ("privacy", "◈", "Приватность"),
        ("appearance", "◐", "Внешний вид"),
        ("performance", "⚡", "Производительность"),
        ("downloads", "⇩", "Загрузки"),
        ("profiles", "☺", "Профили и синхронизация"),
        ("extensions", "◇", "Расширения"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("auralisSettings")
        self._bindings: dict[str, _Binding] = {}
        self._rows: list[SettingRow] = []
        self._section_indexes: dict[str, int] = {}
        self._loading = False

        sidebar = MaterialCard(self, role="surfaceContainer")
        sidebar.setMinimumWidth(240)
        sidebar.setMaximumWidth(285)
        brand = QLabel("Auralis", sidebar)
        brand.setProperty("materialRole", "title")
        section_label = QLabel("Настройки", sidebar)
        section_label.setProperty("materialRole", "body")
        self._search = QLineEdit(sidebar)
        self._search.setPlaceholderText("Поиск настроек")
        self._search.setClearButtonEnabled(True)
        self._navigation = QListWidget(sidebar)
        self._navigation.setSpacing(3)
        self._navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for section_id, symbol, title in self.SECTIONS:
            item = QListWidgetItem(f"  {symbol}   {title}", self._navigation)
            item.setData(Qt.ItemDataRole.UserRole, section_id)
            item.setSizeHint(QSize(210, 44))
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(16, 20, 16, 16)
        side_layout.setSpacing(10)
        side_layout.addWidget(brand)
        side_layout.addWidget(section_label)
        side_layout.addSpacing(7)
        side_layout.addWidget(self._search)
        side_layout.addSpacing(4)
        side_layout.addWidget(self._navigation, 1)

        self._pages = QStackedWidget(self)
        self._pages.setProperty("materialRole", "transparent")
        self._build_pages()
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(sidebar)
        root.addWidget(self._pages, 1)

        self._navigation.currentRowChanged.connect(self._select_row)
        self._search.textChanged.connect(self._filter_rows)
        self._navigation.setCurrentRow(0)

    def settings(self) -> dict[str, object]:
        values = dict(DEFAULT_SETTINGS)
        values.update({key: binding.read() for key, binding in self._bindings.items()})
        return values

    def value(self, key: str, default: object = None) -> object:
        binding = self._bindings.get(key)
        return binding.read() if binding else default

    def set_value(self, key: str, value: object, *, emit: bool = False) -> None:
        binding = self._bindings.get(key)
        if binding is None:
            return
        previous = binding.widget.blockSignals(True)
        try:
            binding.write(value)
        finally:
            binding.widget.blockSignals(previous)
        if emit:
            self._emit_setting(key)

    def load_settings(self, values: Mapping[str, object]) -> None:
        """Load a profile mapping without producing persistence signals."""

        self._loading = True
        try:
            merged = dict(DEFAULT_SETTINGS)
            merged.update(values)
            for key, value in merged.items():
                self.set_value(key, value)
        finally:
            self._loading = False

    def show_section(self, section_id: str) -> None:
        index = self._section_indexes.get(section_id)
        if index is None:
            raise KeyError(f"Unknown settings section: {section_id}")
        self._navigation.setCurrentRow(index)

    def _build_pages(self) -> None:
        builders = {
            "general": self._build_general,
            "privacy": self._build_privacy,
            "appearance": self._build_appearance,
            "performance": self._build_performance,
            "downloads": self._build_downloads,
            "profiles": self._build_profiles,
            "extensions": self._build_extensions,
        }
        for index, (section_id, _symbol, _title) in enumerate(self.SECTIONS):
            page = builders[section_id]()
            self._section_indexes[section_id] = index
            self._pages.addWidget(page)
            self._rows.extend(page.rows)

    def _build_general(self) -> SettingsSection:
        page = SettingsSection("Общие", "Поведение Auralis при запуске и в повседневной работе.")
        home = QLineEdit(page)
        home.setMinimumWidth(260)
        search = self._combo(
            [("Google", "google"), ("DuckDuckGo", "duckduckgo"), ("Bing", "bing"), ("Яндекс", "yandex")]
        )
        language = self._combo([("Русский", "ru"), ("English", "en"), ("Deutsch", "de")])
        restore = MaterialSwitch(page)
        external = MaterialSwitch(page)
        page.add_group(
            "При запуске",
            [
                self._row("Домашняя страница", "Адрес кнопки «Домой» и новых окон.", home, "startup url"),
                self._row("Восстанавливать вкладки", "Продолжать с места завершения предыдущего сеанса.", restore),
            ],
        )
        page.add_group(
            "Язык и поиск",
            [
                self._row("Поисковая система", "Используется для запросов из адресной строки.", search),
                self._row("Язык интерфейса", "Изменение языка применяется после перезапуска.", language),
                self._row("Внешние ссылки в новой вкладке", "Не заменять активную страницу ссылками из других приложений.", external),
            ],
        )
        default_button = MaterialButton("Сделать браузером по умолчанию", page, variant="outlined")
        default_button.clicked.connect(self.defaultBrowserRequested)
        page.add_group("Система", [self._row("Браузер по умолчанию", "Открывать веб-ссылки в Auralis.", default_button)])
        self._bind_line("general.home_page", home)
        self._bind_combo("general.search_engine", search)
        self._bind_combo("general.language", language)
        self._bind_switch("general.restore_session", restore)
        self._bind_switch("general.open_external_links_new_tab", external)
        page.finish()
        return page

    def _build_privacy(self) -> SettingsSection:
        page = SettingsSection("Приватность и безопасность", "Вы сами решаете, какие данные остаются на устройстве.")
        tracking = self._combo([("Стандартная", "standard"), ("Строгая", "strict"), ("Отключена", "off")])
        third_party = MaterialSwitch(page)
        dnt = MaterialSwitch(page)
        safe = MaterialSwitch(page)
        https_only = MaterialSwitch(page)
        page.add_group(
            "Защита от отслеживания",
            [
                self._row("Уровень защиты", "Блокировка известных трекеров и нежелательных сценариев.", tracking, "tracking adblock"),
                self._row("Сторонние cookies", "Блокировать cookies, установленные другими сайтами.", third_party, "cookie"),
                self._row("Сигнал Do Not Track", "Сообщать сайтам о нежелании участвовать в отслеживании.", dnt),
            ],
        )
        page.add_group(
            "Безопасность",
            [
                self._row("Защита от опасных сайтов", "Проверять загрузки и навигацию по локальным спискам угроз.", safe),
                self._row("Только HTTPS", "Предупреждать перед открытием незащищённых страниц.", https_only),
            ],
        )
        clear_button = MaterialButton("Выбрать данные…", page, variant="tonal")
        clear_button.clicked.connect(self.clearBrowsingDataRequested)
        permissions_button = MaterialButton("Разрешения сайтов", page, variant="outlined")
        permissions_button.clicked.connect(self.managePermissionsRequested)
        page.add_group(
            "Данные сайтов",
            [
                self._row("Очистить данные", "История, cookies, кеш и разрешения за выбранный период.", clear_button),
                self._row("Разрешения", "Камера, микрофон, геолокация и уведомления.", permissions_button),
            ],
        )
        self._bind_combo("privacy.tracking_protection", tracking)
        self._bind_switch("privacy.third_party_cookies", third_party)
        self._bind_switch("privacy.do_not_track", dnt)
        self._bind_switch("privacy.safe_browsing", safe)
        self._bind_switch("privacy.https_only", https_only)
        page.finish()
        return page

    def _build_appearance(self) -> SettingsSection:
        page = SettingsSection("Внешний вид", "Динамические цвета Material You подстраивают Auralis под вас.")
        theme = self._combo([("Как в системе", "system"), ("Светлая", "light"), ("Тёмная", "dark")])
        accent = AccentButton(DEFAULT_ACCENT, page)
        density = self._combo([("Компактный", "compact"), ("Комфортный", "comfortable"), ("Просторный", "spacious")])
        scale = QSlider(Qt.Orientation.Horizontal, page)
        scale.setRange(80, 130)
        scale.setSingleStep(5)
        scale.setFixedWidth(180)
        scale.setToolTip("100%")
        scale.valueChanged.connect(lambda value: scale.setToolTip(f"{value}%"))
        bookmarks = MaterialSwitch(page)
        page.add_group(
            "Material You",
            [
                self._row("Тема", "Светлая, тёмная или синхронизированная с системой.", theme),
                self._row("Динамический цвет", "Акцент используется для поверхностей и состояний.", accent, "accent material you"),
            ],
        )
        page.add_group(
            "Интерфейс",
            [
                self._row("Плотность", "Расстояние между элементами и высота полей.", density),
                self._row("Масштаб", "Размер элементов интерфейса от 80 до 130 процентов.", scale),
                self._row("Панель закладок", "Всегда показывать избранные сайты под адресной строкой.", bookmarks),
            ],
        )
        self._bind_combo("appearance.theme", theme)
        self._bind_accent("appearance.accent", accent)
        self._bind_combo("appearance.density", density)
        self._bind_slider("appearance.ui_scale", scale)
        self._bind_switch("appearance.show_bookmarks_bar", bookmarks)
        page.finish()
        return page

    def _build_performance(self) -> SettingsSection:
        page = SettingsSection("Производительность", "Баланс скорости, памяти и энергопотребления.")
        hardware = MaterialSwitch(page)
        saver = MaterialSwitch(page)
        preload = MaterialSwitch(page)
        page.add_group(
            "Система",
            [
                self._row("Аппаратное ускорение", "Использовать GPU для композиции и видео; требуется перезапуск.", hardware, "gpu"),
                self._row("Экономия памяти", "Освобождать неактивные вкладки и восстанавливать их по запросу.", saver),
                self._row("Предзагрузка страниц", "Ускоряет переходы, но использует больше трафика.", preload),
            ],
        )
        clear_cache = MaterialButton("Очистить кеш", page, variant="outlined")
        clear_cache.clicked.connect(self.clearCacheRequested)
        page.add_group("Хранилище", [self._row("Кеш WebEngine", "Удалить временные ресурсы сайтов.", clear_cache, "storage")])
        self._bind_switch("performance.hardware_acceleration", hardware)
        self._bind_switch("performance.memory_saver", saver)
        self._bind_switch("performance.preload_pages", preload)
        page.finish()
        return page

    def _build_downloads(self) -> SettingsSection:
        page = SettingsSection("Загрузки", "Куда сохранять файлы и когда спрашивать подтверждение.")
        directory = QLineEdit(page)
        directory.setReadOnly(True)
        directory.setMinimumWidth(260)
        choose = MaterialIconButton(page)
        choose.setText("…")
        choose.setToolTip("Выбрать папку")
        path_control = QWidget(page)
        path_control.setProperty("materialRole", "transparent")
        path_layout = QHBoxLayout(path_control)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(directory)
        path_layout.addWidget(choose)
        choose.clicked.connect(lambda: self._choose_download_directory(directory))
        ask = MaterialSwitch(page)
        notifications = MaterialSwitch(page)
        open_folder = MaterialButton("Открыть папку", page, variant="outlined")
        open_folder.clicked.connect(self.openDownloadsFolderRequested)
        page.add_group(
            "Сохранение файлов",
            [
                self._row("Папка загрузок", "Основное место для скачанных файлов.", path_control),
                self._row("Спрашивать место сохранения", "Выбирать папку отдельно для каждого файла.", ask),
                self._row("Уведомлять о завершении", "Показывать системное уведомление после загрузки.", notifications),
                self._row("Файлы", "Открыть текущую папку загрузок.", open_folder),
            ],
        )
        self._bind_line("downloads.directory", directory)
        self._bind_switch("downloads.ask_location", ask)
        self._bind_switch("downloads.notifications", notifications)
        page.finish()
        return page

    def _build_profiles(self) -> SettingsSection:
        page = SettingsSection("Профили и синхронизация", "Разделяйте данные пользователей и подключайте облачный backend.")
        profiles = MaterialButton("Управление профилями", page, variant="tonal")
        profiles.clicked.connect(self.profilesRequested)
        sync_switch = MaterialSwitch(page)
        configure = MaterialButton("Настроить синхронизацию", page, variant="outlined")
        configure.clicked.connect(self.syncRequested)
        page.add_group(
            "Профили",
            [self._row("Пользователи Auralis", "Отдельные cookies, история, настройки и аватары.", profiles)],
        )
        page.add_group(
            "Sync",
            [
                self._row("Включить синхронизацию", "Закладки, настройки и вкладки через подключённый backend.", sync_switch),
                self._row("Параметры Sync", "Выбрать категории данных и сервер.", configure),
            ],
        )
        self._bind_switch("sync.enabled", sync_switch)
        page.finish()
        return page

    def _build_extensions(self) -> SettingsSection:
        page = SettingsSection("Расширения", "Управление совместимыми расширениями на основе manifest.json.")
        manage = MaterialButton("Открыть менеджер", page, variant="tonal")
        manage.clicked.connect(self.extensionsRequested)
        page.add_group(
            "Установленные расширения",
            [self._row("Менеджер расширений", "Загрузка из папки, разрешения и режим разработчика.", manage, "manifest chrome")],
        )
        note = QLabel("Поддержка API Chrome Extensions зависит от возможностей Qt WebEngine.", page)
        note.setWordWrap(True)
        note.setProperty("materialRole", "body")
        page._layout.addWidget(note)  # The note belongs to the page, not a setting card.
        page.finish()
        return page

    @staticmethod
    def _combo(items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        combo.setMinimumWidth(170)
        for label, value in items:
            combo.addItem(label, value)
        return combo

    @staticmethod
    def _row(title: str, description: str, control: QWidget, keywords: str = "") -> SettingRow:
        return SettingRow(title, description, control, keywords=keywords)

    def _register(
        self,
        key: str,
        widget: QWidget,
        read: Callable[[], object],
        write: Callable[[object], None],
    ) -> None:
        self._bindings[key] = _Binding(widget, read, write)

    def _bind_switch(self, key: str, widget: MaterialSwitch) -> None:
        self._register(key, widget, widget.isChecked, lambda value: widget.setChecked(bool(value)))
        widget.toggled.connect(lambda _value, key=key: self._emit_setting(key))

    def _bind_line(self, key: str, widget: QLineEdit) -> None:
        self._register(key, widget, widget.text, lambda value: widget.setText(str(value or "")))
        widget.editingFinished.connect(lambda key=key: self._emit_setting(key))

    def _bind_combo(self, key: str, widget: QComboBox) -> None:
        def write(value: object) -> None:
            index = widget.findData(str(value))
            widget.setCurrentIndex(max(0, index))

        self._register(key, widget, widget.currentData, write)
        widget.currentIndexChanged.connect(lambda _index, key=key: self._emit_setting(key))

    def _bind_slider(self, key: str, widget: QSlider) -> None:
        self._register(key, widget, widget.value, lambda value: widget.setValue(int(value)))
        widget.valueChanged.connect(lambda _value, key=key: self._emit_setting(key))

    def _bind_accent(self, key: str, widget: AccentButton) -> None:
        self._register(key, widget, lambda: widget.color, lambda value: widget.set_color(str(value)))
        widget.colorChanged.connect(lambda _value, key=key: self._emit_setting(key))

    def _emit_setting(self, key: str) -> None:
        if self._loading or key not in self._bindings:
            return
        value = self._bindings[key].read()
        self.settingChanged.emit(key, value)
        if key == "appearance.theme":
            self.themeModeChanged.emit(str(value))
        elif key == "appearance.accent":
            self.accentChanged.emit(str(value))
        elif key == "appearance.density":
            self.densityChanged.emit(str(value))

    def _select_row(self, row: int) -> None:
        if 0 <= row < self._pages.count():
            self._pages.setCurrentIndex(row)

    def _filter_rows(self, query: str) -> None:
        needle = query.strip().casefold()
        for row in self._rows:
            row.setVisible(not needle or needle in row.keywords)

    def _choose_download_directory(self, field: QLineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Папка загрузок", field.text())
        if directory:
            field.setText(directory)
            self._emit_setting("downloads.directory")


class SettingsDialog(QDialog):
    """Windowed wrapper around :class:`SettingsPanel`."""

    settingChanged = Signal(str, object)
    clearBrowsingDataRequested = Signal()
    clearCacheRequested = Signal()
    managePermissionsRequested = Signal()
    extensionsRequested = Signal()
    profilesRequested = Signal()
    syncRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Настройки — Auralis Browser")
        self.setMinimumSize(880, 620)
        self.resize(1080, 740)
        self.panel = SettingsPanel(self)
        close_button = MaterialButton("Готово", self, variant="filled")
        close_button.clicked.connect(self.accept)
        footer = QWidget(self)
        footer.setProperty("materialRole", "transparent")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 4, 18, 12)
        footer_layout.addStretch(1)
        footer_layout.addWidget(close_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.panel, 1)
        layout.addWidget(footer)

        self.panel.settingChanged.connect(self.settingChanged)
        self.panel.clearBrowsingDataRequested.connect(self.clearBrowsingDataRequested)
        self.panel.clearCacheRequested.connect(self.clearCacheRequested)
        self.panel.managePermissionsRequested.connect(self.managePermissionsRequested)
        self.panel.extensionsRequested.connect(self.extensionsRequested)
        self.panel.profilesRequested.connect(self.profilesRequested)
        self.panel.syncRequested.connect(self.syncRequested)

    def load_settings(self, values: Mapping[str, object]) -> None:
        self.panel.load_settings(values)

    def settings(self) -> dict[str, object]:
        return self.panel.settings()

    def show_section(self, section_id: str) -> None:
        self.panel.show_section(section_id)


__all__ = [
    "AccentButton",
    "DEFAULT_SETTINGS",
    "MaterialSwitch",
    "SettingRow",
    "SettingsDialog",
    "SettingsPanel",
    "SettingsSection",
]
