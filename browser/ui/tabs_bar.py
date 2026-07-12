"""Material tab strip for Auralis Browser.

The tab strip deliberately stores only presentation metadata.  Page ownership
and navigation lifetime stay in ``core.tabs``/the main window.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QContextMenuEvent, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QTabBar,
    QWidget,
)

from .material_theme import MaterialIconButton


@dataclass(slots=True)
class TabMetadata:
    """Serializable display metadata associated with a tab."""

    tab_id: str
    title: str
    pinned: bool = False
    group: str | None = None
    group_color: str | None = None
    loading: bool = False
    muted: bool = False


class _TabStrip(QTabBar):
    pinRequested = Signal(int, bool)
    duplicateRequested = Signal(int)
    groupRequested = Signal(int, str)
    previewRequested = Signal(int, QPoint)
    previewHidden = Signal()
    newTabRequested = Signal()

    PREVIEW_DELAY_MS = 420

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("auralisTabStrip")
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setMouseTracking(True)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectionBehavior.SelectPreviousTab)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._hovered_index = -1
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(self.PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._show_preview)

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 - Qt API
        hint = super().tabSizeHint(index)
        metadata = self.metadata(index)
        width = 52 if metadata and metadata.pinned else max(112, min(240, hint.width()))
        return QSize(width, max(44, hint.height()))

    def minimumTabSizeHint(self, index: int) -> QSize:  # noqa: N802 - Qt API
        metadata = self.metadata(index)
        return QSize(52 if metadata and metadata.pinned else 92, 44)

    def metadata(self, index: int) -> TabMetadata | None:
        if not 0 <= index < self.count():
            return None
        raw = self.tabData(index)
        if isinstance(raw, TabMetadata):
            return raw
        if isinstance(raw, dict):
            try:
                return TabMetadata(**raw)
            except (TypeError, ValueError):
                return None
        return None

    def set_metadata(self, index: int, metadata: TabMetadata) -> None:
        if not 0 <= index < self.count():
            raise IndexError(f"Tab index out of range: {index}")
        self.setTabData(index, asdict(metadata))
        self._refresh_tab(index)

    def _refresh_tab(self, index: int) -> None:
        metadata = self.metadata(index)
        if metadata is None:
            return
        visible_title = "" if metadata.pinned and not self.tabIcon(index).isNull() else metadata.title
        self.setTabText(index, visible_title)
        details = [metadata.title]
        if metadata.group:
            details.append(f"Группа: {metadata.group}")
        if metadata.pinned:
            details.append("Закреплена")
        if metadata.loading:
            details.append("Загрузка…")
        self.setTabToolTip(index, " · ".join(details))
        close_button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
        if close_button is not None:
            close_button.setVisible(not metadata.pinned)
        if metadata.group_color and QColor(metadata.group_color).isValid():
            self.setTabTextColor(index, QColor(metadata.group_color))
        else:
            self.setTabTextColor(index, QColor())
        self.updateGeometry()
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        index = self.tabAt(event.pos())
        if index < 0:
            menu = QMenu(self)
            new_action = menu.addAction("Новая вкладка")
            if menu.exec(event.globalPos()) is new_action:
                self.newTabRequested.emit()
            return

        metadata = self.metadata(index)
        menu = QMenu(self)
        pin_action = menu.addAction(
            "Открепить вкладку" if metadata and metadata.pinned else "Закрепить вкладку"
        )
        duplicate_action = menu.addAction("Дублировать вкладку")
        group_menu = menu.addMenu("Добавить в группу")
        new_group = group_menu.addAction("Новая группа…")
        remove_group = group_menu.addAction("Убрать из группы")
        remove_group.setEnabled(bool(metadata and metadata.group))
        menu.addSeparator()
        close_action = menu.addAction("Закрыть вкладку")
        close_others_action = menu.addAction("Закрыть другие вкладки")
        close_right_action = menu.addAction("Закрыть вкладки справа")
        close_action.setEnabled(not bool(metadata and metadata.pinned))

        chosen = menu.exec(event.globalPos())
        if chosen is pin_action:
            self.pinRequested.emit(index, not bool(metadata and metadata.pinned))
        elif chosen is duplicate_action:
            self.duplicateRequested.emit(index)
        elif chosen is new_group:
            self.groupRequested.emit(index, "__new__")
        elif chosen is remove_group:
            self.groupRequested.emit(index, "")
        elif chosen is close_action:
            self.tabCloseRequested.emit(index)
        elif chosen is close_others_action:
            for candidate in range(self.count() - 1, -1, -1):
                candidate_meta = self.metadata(candidate)
                if candidate != index and not bool(candidate_meta and candidate_meta.pinned):
                    self.tabCloseRequested.emit(candidate)
        elif chosen is close_right_action:
            for candidate in range(self.count() - 1, index, -1):
                candidate_meta = self.metadata(candidate)
                if not bool(candidate_meta and candidate_meta.pinned):
                    self.tabCloseRequested.emit(candidate)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        index = self.tabAt(event.position().toPoint())
        if index != self._hovered_index:
            if self._hovered_index >= 0:
                self.previewHidden.emit()
            self._hovered_index = index
            self._preview_timer.stop()
            if index >= 0:
                self._preview_timer.start()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._preview_timer.stop()
        self._hovered_index = -1
        self.previewHidden.emit()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            metadata = self.metadata(index)
            if index >= 0 and not bool(metadata and metadata.pinned):
                self.tabCloseRequested.emit(index)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.tabAt(event.position().toPoint()) < 0:
            self.newTabRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _show_preview(self) -> None:
        if not 0 <= self._hovered_index < self.count():
            return
        anchor = self.mapToGlobal(self.tabRect(self._hovered_index).bottomLeft())
        self.previewRequested.emit(self._hovered_index, anchor)


class MaterialTabBar(QWidget):
    """Browser tab bar with pin/group/reorder/preview integration signals.

    ``MaterialTabBar`` does not remove tabs after ``closeTabRequested``.  The
    owner first tears down the web page and then calls :meth:`remove_tab`, which
    keeps WebEngine lifetime management explicit.
    """

    newTabRequested = Signal()
    tabActivated = Signal(int)
    closeTabRequested = Signal(int)
    pinTabRequested = Signal(int, bool)
    duplicateTabRequested = Signal(int)
    groupTabRequested = Signal(int, str)
    tabMoved = Signal(int, int)
    previewRequested = Signal(int, QPoint)
    previewHidden = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("materialTabBar")
        self.setProperty("materialRole", "surfaceContainer")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._strip = _TabStrip(self)
        self._suppress_move_signal = False
        self._normalizing_move = False
        self._new_button = MaterialIconButton(self, variant="icon")
        self._new_button.setText("+")
        self._new_button.setToolTip("Новая вкладка (Ctrl+T)")
        self._new_button.setAccessibleName("Новая вкладка")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        layout.addWidget(self._strip, 1)
        layout.addWidget(self._new_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._new_button.clicked.connect(self.newTabRequested)
        self._strip.newTabRequested.connect(self.newTabRequested)
        self._strip.currentChanged.connect(self._emit_activated)
        self._strip.tabCloseRequested.connect(self.closeTabRequested)
        self._strip.pinRequested.connect(self.pinTabRequested)
        self._strip.duplicateRequested.connect(self.duplicateTabRequested)
        self._strip.groupRequested.connect(self.groupTabRequested)
        self._strip.tabMoved.connect(self._handle_tab_moved)
        self._strip.previewRequested.connect(self.previewRequested)
        self._strip.previewHidden.connect(self.previewHidden)

    @property
    def native_bar(self) -> QTabBar:
        """Read-only access for rare Qt integrations (for example shortcuts)."""

        return self._strip

    def count(self) -> int:
        return self._strip.count()

    def current_index(self) -> int:
        return self._strip.currentIndex()

    def set_current_index(self, index: int) -> None:
        if index != -1 and not 0 <= index < self.count():
            raise IndexError(f"Tab index out of range: {index}")
        self._strip.setCurrentIndex(index)

    def add_tab(
        self,
        title: str,
        icon: QIcon | None = None,
        *,
        pinned: bool = False,
        group: str | None = None,
        group_color: str | None = None,
        tab_id: str | None = None,
        make_current: bool = True,
    ) -> int:
        """Append a tab and return its current visual index."""

        index = self._strip.addTab(icon or QIcon(), title)
        metadata = TabMetadata(
            tab_id=tab_id or uuid4().hex,
            title=title or "Новая вкладка",
            pinned=pinned,
            group=group,
            group_color=group_color,
        )
        self._strip.set_metadata(index, metadata)
        if pinned:
            target = self._pinned_count(exclude=index)
            if target != index:
                self._move_without_signal(index, target)
                index = target
        if make_current:
            self._strip.setCurrentIndex(index)
        return index

    # Alias used by a few controller styles.
    add_browser_tab = add_tab

    def update_tab(
        self,
        index: int,
        *,
        title: str | None = None,
        icon: QIcon | None = None,
        pinned: bool | None = None,
        group: str | None = None,
        group_color: str | None = None,
        loading: bool | None = None,
        muted: bool | None = None,
    ) -> int:
        """Update presentation state and return the potentially new index."""

        metadata = self._require_metadata(index)
        was_pinned = metadata.pinned
        if title is not None:
            metadata.title = title or "Новая вкладка"
        if pinned is not None:
            metadata.pinned = pinned
        if group is not None:
            metadata.group = group or None
        if group_color is not None:
            metadata.group_color = group_color or None
        if loading is not None:
            metadata.loading = loading
        if muted is not None:
            metadata.muted = muted
        if icon is not None:
            self._strip.setTabIcon(index, icon)
        self._strip.set_metadata(index, metadata)

        if pinned is not None and pinned != was_pinned:
            target = (
                self._pinned_count(exclude=index) if pinned else max(0, self._pinned_count(exclude=index))
            )
            if target != index:
                self._move_without_signal(index, target)
                index = target
        return index

    def set_tab_pinned(self, index: int, pinned: bool) -> int:
        return self.update_tab(index, pinned=pinned)

    def set_tab_group(self, index: int, group: str | None, color: str | None = None) -> int:
        return self.update_tab(index, group=group or "", group_color=color or "")

    def remove_tab(self, index: int) -> TabMetadata:
        metadata = self._require_metadata(index)
        self._strip.removeTab(index)
        return metadata

    def clear(self) -> None:
        while self.count():
            self._strip.removeTab(self.count() - 1)

    def tab_metadata(self, index: int) -> TabMetadata | None:
        return self._strip.metadata(index)

    def index_for_id(self, tab_id: str) -> int:
        for index in range(self.count()):
            metadata = self._strip.metadata(index)
            if metadata and metadata.tab_id == tab_id:
                return index
        return -1

    def set_tab_enabled(self, index: int, enabled: bool) -> None:
        self._strip.setTabEnabled(index, enabled)

    def _pinned_count(self, *, exclude: int = -1) -> int:
        return sum(
            1
            for index in range(self.count())
            if index != exclude and (metadata := self._strip.metadata(index)) is not None and metadata.pinned
        )

    def _require_metadata(self, index: int) -> TabMetadata:
        metadata = self._strip.metadata(index)
        if metadata is None:
            raise IndexError(f"Tab index out of range or without metadata: {index}")
        return metadata

    def _emit_activated(self, index: int) -> None:
        if index >= 0:
            self.tabActivated.emit(index)

    def _move_without_signal(self, source: int, destination: int) -> None:
        self._suppress_move_signal = True
        try:
            self._strip.moveTab(source, destination)
        finally:
            self._suppress_move_signal = False

    def _handle_tab_moved(self, source: int, destination: int) -> None:
        if self._suppress_move_signal or self._normalizing_move:
            return
        metadata = self._strip.metadata(destination)
        if metadata is None:
            return
        pinned_count = self._pinned_count()
        corrected = destination
        if metadata.pinned and destination >= pinned_count:
            corrected = max(0, pinned_count - 1)
        elif not metadata.pinned and destination < pinned_count:
            corrected = pinned_count
        if corrected != destination:
            self._normalizing_move = True
            try:
                self._strip.moveTab(destination, corrected)
            finally:
                self._normalizing_move = False
        self.tabMoved.emit(source, corrected)


# Friendly alias for controllers using the file's historical name.
TabsBar = MaterialTabBar


__all__ = ["MaterialTabBar", "TabMetadata", "TabsBar"]
