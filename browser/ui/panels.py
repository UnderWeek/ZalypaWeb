"""Material side panels for bookmarks, history and downloads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .material_theme import MaterialButton, MaterialCard, MaterialIconButton


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _human_bytes(value: int | float) -> str:
    size = max(0.0, float(value))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


@dataclass(frozen=True, slots=True)
class BookmarkItem:
    bookmark_id: object
    title: str
    url: str
    folder_id: object | None = None
    folder_name: str = ""
    created_at: datetime | None = None

    @classmethod
    def from_value(cls, value: "BookmarkItem | dict[str, object]") -> "BookmarkItem":
        if isinstance(value, cls):
            return value
        return cls(
            bookmark_id=value.get("bookmark_id", value.get("id")),
            title=str(value.get("title") or value.get("name") or value.get("url") or "Закладка"),
            url=str(value.get("url") or ""),
            folder_id=value.get("folder_id"),
            folder_name=str(value.get("folder_name") or value.get("folder") or ""),
            created_at=_coerce_datetime(value.get("created_at")),
        )


@dataclass(frozen=True, slots=True)
class HistoryItem:
    history_id: object
    title: str
    url: str
    visited_at: datetime | None = None
    visit_count: int = 1

    @classmethod
    def from_value(cls, value: "HistoryItem | dict[str, object]") -> "HistoryItem":
        if isinstance(value, cls):
            return value
        return cls(
            history_id=value.get("history_id", value.get("id")),
            title=str(value.get("title") or value.get("url") or "Страница"),
            url=str(value.get("url") or ""),
            visited_at=_coerce_datetime(value.get("visited_at", value.get("date"))),
            visit_count=int(value.get("visit_count") or 1),
        )


@dataclass(frozen=True, slots=True)
class DownloadItem:
    download_id: object
    file_name: str
    url: str = ""
    path: str = ""
    received_bytes: int = 0
    total_bytes: int = 0
    speed_bytes: float = 0.0
    state: str = "queued"
    started_at: datetime | None = None
    error: str = ""

    @classmethod
    def from_value(cls, value: "DownloadItem | dict[str, object]") -> "DownloadItem":
        if isinstance(value, cls):
            return value
        path = str(value.get("path") or value.get("target_path") or "")
        return cls(
            download_id=value.get("download_id", value.get("id")),
            file_name=str(value.get("file_name") or value.get("name") or Path(path).name or "Загрузка"),
            url=str(value.get("url") or ""),
            path=path,
            received_bytes=int(value.get("received_bytes") or value.get("bytes_received") or 0),
            total_bytes=int(value.get("total_bytes") or value.get("bytes_total") or 0),
            speed_bytes=float(value.get("speed_bytes") or value.get("speed") or 0),
            state=str(value.get("state") or "queued").lower(),
            started_at=_coerce_datetime(value.get("started_at")),
            error=str(value.get("error") or ""),
        )


class MaterialSidePanel(QWidget):
    """Common panel shell with title, search and close request."""

    closeRequested = Signal()
    searchChanged = Signal(str)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("materialSidePanel")
        self.setProperty("materialRole", "surface")
        self.setMinimumWidth(340)
        self.setMaximumWidth(560)
        self.resize(410, 680)
        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(16, 16, 16, 16)
        self.content_layout.setSpacing(12)
        header = QWidget(self)
        header.setProperty("materialRole", "transparent")
        heading = QLabel(title, header)
        heading.setProperty("materialRole", "title")
        close = MaterialIconButton(header)
        close.setText("×")
        close.setToolTip("Закрыть панель")
        close.clicked.connect(self.closeRequested)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(heading)
        header_layout.addStretch(1)
        header_layout.addWidget(close)
        self.search = QLineEdit(self)
        self.search.setProperty("materialRole", "search")
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(f"Поиск: {title.casefold()}")
        self.search.textChanged.connect(self.searchChanged)
        self.content_layout.addWidget(header)
        self.content_layout.addWidget(self.search)


class BookmarksPanel(MaterialSidePanel):
    openRequested = Signal(str, bool)
    addRequested = Signal()
    editRequested = Signal(object)
    deleteRequested = Signal(object)
    newFolderRequested = Signal()
    importRequested = Signal()
    exportRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Закладки", parent)
        self._items: list[BookmarkItem] = []
        actions = QWidget(self)
        actions.setProperty("materialRole", "transparent")
        add = MaterialButton("Добавить", actions, variant="tonal")
        folder = MaterialIconButton(actions)
        folder.setText("▣")
        folder.setToolTip("Новая папка")
        more = MaterialIconButton(actions)
        more.setText("⋮")
        more.setToolTip("Импорт и экспорт")
        add.clicked.connect(self.addRequested)
        folder.clicked.connect(self.newFolderRequested)
        more.clicked.connect(lambda: self._show_more_menu(more))
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(add)
        action_layout.addStretch(1)
        action_layout.addWidget(folder)
        action_layout.addWidget(more)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(18)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.content_layout.addWidget(actions)
        self.content_layout.addWidget(self.tree, 1)
        self.searchChanged.connect(self._filter)

    def set_items(self, values: list[BookmarkItem | dict[str, object]]) -> None:
        self._items = [BookmarkItem.from_value(value) for value in values]
        self.tree.clear()
        folders: dict[object, QTreeWidgetItem] = {}
        loose = QTreeWidgetItem(["Без папки"])
        loose.setData(0, Qt.ItemDataRole.UserRole, {"type": "folder", "id": None})
        for bookmark in self._items:
            if bookmark.folder_id is not None and bookmark.folder_id not in folders:
                folder = QTreeWidgetItem([bookmark.folder_name or "Папка"])
                folder.setData(0, Qt.ItemDataRole.UserRole, {"type": "folder", "id": bookmark.folder_id})
                folders[bookmark.folder_id] = folder
                self.tree.addTopLevelItem(folder)
            parent = folders.get(bookmark.folder_id, loose)
            child = QTreeWidgetItem([bookmark.title])
            child.setToolTip(0, bookmark.url)
            child.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {"type": "bookmark", "id": bookmark.bookmark_id, "url": bookmark.url, "title": bookmark.title},
            )
            parent.addChild(child)
        if loose.childCount():
            self.tree.insertTopLevelItem(0, loose)
        self.tree.expandAll()
        self._filter(self.search.text())

    def selected_id(self) -> object | None:
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return data.get("id") if isinstance(data, dict) and data.get("type") == "bookmark" else None

    def _open_item(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and data.get("type") == "bookmark":
            self.openRequested.emit(str(data["url"]), False)

    def _context_menu(self, position) -> None:  # noqa: ANN001
        item = self.tree.itemAt(position)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or data.get("type") != "bookmark":
            return
        menu = QMenu(self)
        open_here = menu.addAction("Открыть")
        open_new = menu.addAction("Открыть в новой вкладке")
        menu.addSeparator()
        edit = menu.addAction("Изменить")
        delete = menu.addAction("Удалить")
        selected = menu.exec(self.tree.viewport().mapToGlobal(position))
        if selected is open_here:
            self.openRequested.emit(str(data["url"]), False)
        elif selected is open_new:
            self.openRequested.emit(str(data["url"]), True)
        elif selected is edit:
            self.editRequested.emit(data["id"])
        elif selected is delete:
            self.deleteRequested.emit(data["id"])

    def _show_more_menu(self, anchor: QWidget) -> None:
        menu = QMenu(self)
        import_action = menu.addAction("Импортировать закладки…")
        export_action = menu.addAction("Экспортировать закладки…")
        selected = menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        if selected is import_action:
            self.importRequested.emit()
        elif selected is export_action:
            self.exportRequested.emit()

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for top_index in range(self.tree.topLevelItemCount()):
            folder = self.tree.topLevelItem(top_index)
            visible_children = 0
            for child_index in range(folder.childCount()):
                child = folder.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                haystack = f"{child.text(0)} {data.get('url', '') if isinstance(data, dict) else ''}".casefold()
                visible = not needle or needle in haystack
                child.setHidden(not visible)
                visible_children += int(visible)
            folder.setHidden(bool(needle) and visible_children == 0)


class HistoryPanel(MaterialSidePanel):
    openRequested = Signal(str, bool)
    deleteRequested = Signal(object)
    clearRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("История", parent)
        self._items: list[HistoryItem] = []
        clear = MaterialButton("Очистить историю", self, variant="outlined")
        clear.clicked.connect(self.clearRequested)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemDoubleClicked.connect(self._open)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.content_layout.addWidget(clear, 0, Qt.AlignmentFlag.AlignRight)
        self.content_layout.addWidget(self.tree, 1)
        self.searchChanged.connect(self._filter)

    def set_items(self, values: list[HistoryItem | dict[str, object]]) -> None:
        self._items = [HistoryItem.from_value(value) for value in values]
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        today = datetime.now().date()
        for entry in sorted(self._items, key=lambda item: item.visited_at or datetime.min, reverse=True):
            date = entry.visited_at.date() if entry.visited_at else None
            if date == today:
                label = "Сегодня"
            elif date and (today - date).days == 1:
                label = "Вчера"
            elif date:
                label = date.strftime("%d.%m.%Y")
            else:
                label = "Ранее"
            group = groups.get(label)
            if group is None:
                group = QTreeWidgetItem([label])
                group.setData(0, Qt.ItemDataRole.UserRole, {"type": "group"})
                groups[label] = group
                self.tree.addTopLevelItem(group)
            time_label = entry.visited_at.strftime("%H:%M") if entry.visited_at else ""
            child = QTreeWidgetItem([f"{time_label}   {entry.title}"])
            child.setToolTip(0, entry.url)
            child.setData(0, Qt.ItemDataRole.UserRole, {"type": "history", "id": entry.history_id, "url": entry.url})
            group.addChild(child)
        self.tree.expandAll()
        self._filter(self.search.text())

    def _open(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and data.get("type") == "history":
            self.openRequested.emit(str(data["url"]), False)

    def _context_menu(self, position) -> None:  # noqa: ANN001
        item = self.tree.itemAt(position)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(data, dict) or data.get("type") != "history":
            return
        menu = QMenu(self)
        open_new = menu.addAction("Открыть в новой вкладке")
        delete = menu.addAction("Удалить запись")
        selected = menu.exec(self.tree.viewport().mapToGlobal(position))
        if selected is open_new:
            self.openRequested.emit(str(data["url"]), True)
        elif selected is delete:
            self.deleteRequested.emit(data["id"])

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for top_index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(top_index)
            visible_children = 0
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                haystack = f"{child.text(0)} {data.get('url', '') if isinstance(data, dict) else ''}".casefold()
                visible = not needle or needle in haystack
                child.setHidden(not visible)
                visible_children += int(visible)
            group.setHidden(bool(needle) and visible_children == 0)


class DownloadRow(MaterialCard):
    pauseRequested = Signal(object)
    resumeRequested = Signal(object)
    cancelRequested = Signal(object)
    openRequested = Signal(object)
    showInFolderRequested = Signal(object)
    removeRequested = Signal(object)

    ACTIVE_STATES = {"queued", "in_progress", "downloading", "paused"}

    def __init__(self, item: DownloadItem, parent: QWidget | None = None) -> None:
        super().__init__(parent, role="surfaceContainer")
        self.item = item
        self.title = QLabel(self)
        self.title.setProperty("materialRole", "subtitle")
        self.status = QLabel(self)
        self.status.setProperty("materialRole", "body")
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.primary = MaterialButton("", self, variant="tonal")
        self.secondary = MaterialIconButton(self)
        self.secondary.setText("×")
        self.primary.clicked.connect(self._primary_action)
        self.secondary.clicked.connect(self._secondary_action)
        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(self.primary)
        action_layout.addWidget(self.secondary)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addLayout(action_layout)
        self.update_item(item)

    def update_item(self, item: DownloadItem) -> None:
        self.item = item
        self.title.setText(item.file_name)
        total = item.total_bytes
        received = item.received_bytes
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, round(received / total * 100)))
        elif item.state in {"in_progress", "downloading"}:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        labels = {
            "queued": "Ожидание",
            "in_progress": "Загружается",
            "downloading": "Загружается",
            "paused": "Приостановлена",
            "completed": "Готово",
            "cancelled": "Отменена",
            "failed": "Ошибка",
        }
        state_text = labels.get(item.state, item.state.capitalize())
        if item.state in {"in_progress", "downloading"}:
            state_text += f" · {_human_bytes(received)} из {_human_bytes(total) if total else '—'}"
            if item.speed_bytes:
                state_text += f" · {_human_bytes(item.speed_bytes)}/с"
        elif item.error:
            state_text += f" · {item.error}"
        elif total:
            state_text += f" · {_human_bytes(total)}"
        self.status.setText(state_text)
        self.progress.setVisible(item.state not in {"completed", "cancelled", "failed"})
        if item.state == "paused":
            self.primary.setText("Продолжить")
        elif item.state in {"queued", "in_progress", "downloading"}:
            self.primary.setText("Пауза")
        elif item.state == "completed":
            self.primary.setText("Открыть")
        else:
            self.primary.setText("Убрать")
        self.secondary.setToolTip("Отменить" if item.state in self.ACTIVE_STATES else "Показать в папке")

    def _primary_action(self) -> None:
        if self.item.state == "paused":
            self.resumeRequested.emit(self.item.download_id)
        elif self.item.state in {"queued", "in_progress", "downloading"}:
            self.pauseRequested.emit(self.item.download_id)
        elif self.item.state == "completed":
            self.openRequested.emit(self.item.download_id)
        else:
            self.removeRequested.emit(self.item.download_id)

    def _secondary_action(self) -> None:
        if self.item.state in self.ACTIVE_STATES:
            self.cancelRequested.emit(self.item.download_id)
        else:
            self.showInFolderRequested.emit(self.item.download_id)


class DownloadsPanel(MaterialSidePanel):
    pauseRequested = Signal(object)
    resumeRequested = Signal(object)
    cancelRequested = Signal(object)
    openRequested = Signal(object)
    showInFolderRequested = Signal(object)
    removeRequested = Signal(object)
    clearCompletedRequested = Signal()
    openFolderRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Загрузки", parent)
        self._items: dict[object, DownloadItem] = {}
        self._rows: dict[object, DownloadRow] = {}
        actions = QWidget(self)
        actions.setProperty("materialRole", "transparent")
        open_folder = MaterialButton("Папка", actions, variant="outlined")
        clear = MaterialButton("Убрать завершённые", actions, variant="text")
        open_folder.clicked.connect(self.openFolderRequested)
        clear.clicked.connect(self.clearCompletedRequested)
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(open_folder)
        action_layout.addStretch(1)
        action_layout.addWidget(clear)
        self.list = QListWidget(self)
        self.list.setSpacing(8)
        self.content_layout.addWidget(actions)
        self.content_layout.addWidget(self.list, 1)
        self.searchChanged.connect(self._filter)

    def set_items(self, values: list[DownloadItem | dict[str, object]]) -> None:
        self.list.clear()
        self._items.clear()
        self._rows.clear()
        for value in values:
            self._append(DownloadItem.from_value(value))
        self._filter(self.search.text())

    def update_download(self, download_id: object, **changes: object) -> None:
        item = self._items.get(download_id)
        if item is None:
            changes.setdefault("download_id", download_id)
            changes.setdefault("file_name", str(changes.get("path") or "Загрузка"))
            self._append(DownloadItem.from_value(changes))
            return
        allowed = {name for name in DownloadItem.__dataclass_fields__ if name != "download_id"}
        normalized = {key: value for key, value in changes.items() if key in allowed}
        if "started_at" in normalized:
            normalized["started_at"] = _coerce_datetime(normalized["started_at"])
        item = replace(item, **normalized)
        self._items[download_id] = item
        self._rows[download_id].update_item(item)
        self._filter(self.search.text())

    def remove_download(self, download_id: object) -> None:
        row = self._rows.pop(download_id, None)
        self._items.pop(download_id, None)
        if row is None:
            return
        for index in range(self.list.count()):
            item = self.list.item(index)
            if self.list.itemWidget(item) is row:
                self.list.takeItem(index)
                row.deleteLater()
                return

    def _append(self, item: DownloadItem) -> None:
        self._items[item.download_id] = item
        row = DownloadRow(item, self.list)
        row.pauseRequested.connect(self.pauseRequested)
        row.resumeRequested.connect(self.resumeRequested)
        row.cancelRequested.connect(self.cancelRequested)
        row.openRequested.connect(self.openRequested)
        row.showInFolderRequested.connect(self.showInFolderRequested)
        row.removeRequested.connect(self.removeRequested)
        list_item = QListWidgetItem(self.list)
        list_item.setSizeHint(row.sizeHint())
        list_item.setData(Qt.ItemDataRole.UserRole, item.download_id)
        self.list.addItem(list_item)
        self.list.setItemWidget(list_item, row)
        self._rows[item.download_id] = row

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for index in range(self.list.count()):
            list_item = self.list.item(index)
            identifier = list_item.data(Qt.ItemDataRole.UserRole)
            item = self._items.get(identifier)
            visible = bool(item) and (
                not needle or needle in f"{item.file_name} {item.url} {item.path}".casefold()
            )
            list_item.setHidden(not visible)


__all__ = [
    "BookmarkItem",
    "BookmarksPanel",
    "DownloadItem",
    "DownloadRow",
    "DownloadsPanel",
    "HistoryItem",
    "HistoryPanel",
    "MaterialSidePanel",
]
