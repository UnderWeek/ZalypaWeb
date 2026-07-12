"""Main Auralis browser window and feature coordination.

The window is intentionally a coordinator: Chromium lifetime belongs to
``core.browser_engine``, serializable tab state to ``core.tabs`` and durable
records to the repositories in ``database``.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import qrcode
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from browser.core.browser_engine import BrowserDownload, BrowserEngine, BrowserView
from browser.core.profiles import BrowserProfile, ProfileManager
from browser.core.security import PermissionDecision as StoredPermissionDecision
from browser.core.security import PermissionType, SecurityManager
from browser.core.tabs import DEFAULT_NEW_TAB_URL, TabManager, TabState
from browser.database.bookmarks import BookmarksRepository
from browser.database.connection import SQLiteDatabase
from browser.database.downloads import DownloadsRepository, DownloadStatus
from browser.database.history import HistoryRepository
from browser.database.settings import SettingsRepository
from browser.services.adblock import AdBlocker
from browser.services.extensions import ExtensionManager
from browser.services.filter_updater import update_filter_subscription

from .bookmarks_bar import BookmarksBar
from .dialogs import (
    BookmarkDialog,
    ClearBrowsingDataDialog,
    PermissionDecision,
    PermissionDialog,
    SiteInformationDialog,
)
from .find_bar import FindBar
from .material_theme import ThemeManager
from .navigation_bar import NavigationBar
from .overlays import Snackbar, TabPreview
from .panels import BookmarksPanel, DownloadsPanel, HistoryPanel
from .settings import DEFAULT_SETTINGS, SettingsDialog
from .tabs_bar import MaterialTabBar

LOGGER = logging.getLogger(__name__)
SETTINGS_NAMESPACE = "browser"
SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "yandex": "https://yandex.ru/search/?text={query}",
}


@dataclass(slots=True)
class BrowserContext:
    """Profile-scoped dependencies used by one window."""

    profile_manager: ProfileManager
    profile: BrowserProfile
    database: SQLiteDatabase
    history: HistoryRepository
    bookmarks: BookmarksRepository
    downloads: DownloadsRepository
    settings: SettingsRepository
    security: SecurityManager
    adblocker: AdBlocker
    extensions: ExtensionManager
    engine: BrowserEngine
    theme: ThemeManager


class BrowserWindow(QMainWindow):
    """A complete multi-tab Material 3 browser shell."""

    profileWindowRequested = Signal(str, bool)

    def __init__(self, context: BrowserContext, *, incognito: bool = False) -> None:
        super().__init__()
        self.context = context
        self.incognito = incognito
        self.settings_values = dict(DEFAULT_SETTINGS)
        self.settings_values.update(context.settings.get_all(namespace=SETTINGS_NAMESPACE))
        self.setObjectName("auralisMainWindow")
        suffix = " — приватный режим" if incognito else ""
        self.setWindowTitle(f"Auralis Browser{suffix}")
        self.resize(1380, 880)
        self.setMinimumSize(840, 560)

        self.tab_manager = TabManager(
            context.profile.paths.root / ("private-session.json" if incognito else "session.json"),
            parent=self,
        )
        self.views: dict[str, BrowserView] = {}
        self._view_tab_ids: dict[int, str] = {}
        self._downloads_by_key: dict[object, BrowserDownload] = {}
        self._download_db_ids: dict[str, int] = {}
        self._download_write_times: dict[str, float] = {}
        self._settings_dialog: SettingsDialog | None = None
        self._devtools_windows: list[QMainWindow] = []
        self._full_screen = False
        self._closing = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

        self.tab_bar = MaterialTabBar(self)
        self.navigation = NavigationBar(self)
        self.navigation.set_profile(
            context.profile.name,
            str(context.profile.avatar_path) if context.profile.avatar_path else None,
        )
        self.find_bar = FindBar(self)
        self.bookmarks_bar = BookmarksBar(self)
        self.web_stack = QStackedWidget(self)
        self.web_stack.setObjectName("webStack")
        self.bookmarks_panel = BookmarksPanel(self)
        self.history_panel = HistoryPanel(self)
        self.downloads_panel = DownloadsPanel(self)
        self.side_stack = QStackedWidget(self)
        for panel in (self.bookmarks_panel, self.history_panel, self.downloads_panel):
            self.side_stack.addWidget(panel)
        self.side_stack.setFixedWidth(390)
        self.side_stack.hide()
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.web_stack)
        self.splitter.addWidget(self.side_stack)
        self.splitter.setStretchFactor(0, 1)

        shell = QWidget(self)
        shell.setObjectName("browserShell")
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.tab_bar)
        layout.addWidget(self.navigation)
        layout.addWidget(self.bookmarks_bar)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(shell)
        self.snackbar = Snackbar(shell)
        self.preview = TabPreview(self)

        self._suggestion_timer = QTimer(self)
        self._suggestion_timer.setSingleShot(True)
        self._suggestion_timer.setInterval(110)
        self._suggestion_timer.timeout.connect(self._refresh_suggestions)
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(60_000)
        self._memory_timer.timeout.connect(self._apply_memory_saver)
        self._memory_timer.start()

        self._wire_ui()
        self.bookmarks_bar.setVisible(
            bool(self.settings_values.get("appearance.show_bookmarks_bar", False))
        )
        self._refresh_bookmarks_bar()
        self._install_shortcuts()
        self._load_persisted_downloads()
        self._restore_or_create_session()

    @property
    def current_tab_id(self) -> str | None:
        metadata = self.tab_bar.tab_metadata(self.tab_bar.current_index()) if self.tab_bar.count() else None
        return metadata.tab_id if metadata else None

    @property
    def current_view(self) -> BrowserView | None:
        tab_id = self.current_tab_id
        return self.views.get(tab_id) if tab_id else None

    def _wire_ui(self) -> None:
        self.tab_bar.newTabRequested.connect(lambda: self.create_tab())
        self.tab_bar.tabActivated.connect(self._activate_tab_index)
        self.tab_bar.closeTabRequested.connect(self._close_tab_index)
        self.tab_bar.pinTabRequested.connect(self._pin_tab)
        self.tab_bar.duplicateTabRequested.connect(self._duplicate_tab_index)
        self.tab_bar.groupTabRequested.connect(self._group_tab)
        self.tab_bar.tabMoved.connect(self.tab_manager.move)
        self.tab_bar.previewRequested.connect(self._show_tab_preview)
        self.tab_bar.previewHidden.connect(self.preview.hide)

        self.navigation.backRequested.connect(lambda: self.current_view and self.current_view.back())
        self.navigation.forwardRequested.connect(lambda: self.current_view and self.current_view.forward())
        self.navigation.reloadRequested.connect(lambda: self.current_view and self.current_view.reload())
        self.navigation.stopRequested.connect(lambda: self.current_view and self.current_view.stop())
        self.navigation.homeRequested.connect(self._go_home)
        self.navigation.bookmarkRequested.connect(self._toggle_bookmark)
        self.navigation.navigateRequested.connect(self.navigate)
        self.navigation.suggestionQueryChanged.connect(self._queue_suggestions)
        self.navigation.menuRequested.connect(self._show_main_menu)
        self.navigation.siteInfoRequested.connect(self._show_site_information)
        self.navigation.profileRequested.connect(self._show_profiles)
        self.bookmarks_bar.navigateRequested.connect(self.navigate)
        self.bookmarks_bar.manageRequested.connect(
            lambda: self._show_side_panel(self.bookmarks_panel)
        )

        self.find_bar.findRequested.connect(self._find_text)
        self.find_bar.closeRequested.connect(
            lambda: self.current_view and self.current_view.page().findText("")
        )
        self.context.engine.download_created.connect(self._on_download_created)
        self.context.engine.permission_requested.connect(self._on_permission_requested)
        self.context.engine.notification_requested.connect(self._on_notification)
        self.context.theme.themeChanged.connect(lambda _palette: self._apply_web_theme())

        self.bookmarks_panel.closeRequested.connect(self.side_stack.hide)
        self.bookmarks_panel.openRequested.connect(self._open_from_panel)
        self.bookmarks_panel.addRequested.connect(self._toggle_bookmark)
        self.bookmarks_panel.editRequested.connect(self._edit_bookmark)
        self.bookmarks_panel.deleteRequested.connect(self._delete_bookmark)
        self.bookmarks_panel.newFolderRequested.connect(self._new_bookmark_folder)
        self.bookmarks_panel.importRequested.connect(self._import_bookmarks)
        self.bookmarks_panel.exportRequested.connect(self._export_bookmarks)
        self.history_panel.closeRequested.connect(self.side_stack.hide)
        self.history_panel.openRequested.connect(self._open_from_panel)
        self.history_panel.deleteRequested.connect(self._delete_history_entry)
        self.history_panel.clearRequested.connect(self._show_clear_data)
        self.downloads_panel.closeRequested.connect(self.side_stack.hide)
        self.downloads_panel.pauseRequested.connect(self._pause_download)
        self.downloads_panel.resumeRequested.connect(self._resume_download)
        self.downloads_panel.cancelRequested.connect(self._cancel_download)
        self.downloads_panel.openRequested.connect(self._open_download)
        self.downloads_panel.showInFolderRequested.connect(self._show_download_in_folder)
        self.downloads_panel.removeRequested.connect(self._remove_download_record)
        self.downloads_panel.clearCompletedRequested.connect(self._clear_completed_downloads)
        self.downloads_panel.openFolderRequested.connect(self._open_downloads_folder)

    def _restore_or_create_session(self) -> None:
        restore = bool(self.settings_values.get("general.restore_session", True)) and not self.incognito
        restored = restore and self.tab_manager.load_session()
        if restored:
            for state in self.tab_manager.tabs:
                self._materialize_tab(state, make_current=state.id == self.tab_manager.current_id)
            if self.tab_manager.current_id:
                self._activate_tab_id(self.tab_manager.current_id)
        else:
            self.create_tab(DEFAULT_NEW_TAB_URL)

    def create_tab(
        self,
        url: str = DEFAULT_NEW_TAB_URL,
        *,
        background: bool = False,
        pinned: bool = False,
        group_id: str | None = None,
    ) -> BrowserView:
        state = self.tab_manager.add(
            url or DEFAULT_NEW_TAB_URL,
            pinned=pinned,
            group_id=group_id,
            make_current=not background,
        )
        return self._materialize_tab(state, make_current=not background)

    def _materialize_tab(self, state: TabState, *, make_current: bool) -> BrowserView:
        view = self.context.engine.create_view(self.context.profile.id, parent=self.web_stack)
        view.setProperty("tabId", state.id)
        view.setZoomFactor(state.zoom)
        view.new_window_factory = lambda _kind: self.create_tab("about:blank")
        view.titleChanged.connect(lambda title, tab_id=state.id: self._page_title_changed(tab_id, title))
        view.urlChanged.connect(lambda url, tab_id=state.id: self._page_url_changed(tab_id, url))
        view.iconChanged.connect(lambda icon, tab_id=state.id: self._page_icon_changed(tab_id, icon))
        view.loadStarted.connect(lambda tab_id=state.id: self._page_load_started(tab_id))
        view.loadProgress.connect(lambda value, tab_id=state.id: self._page_load_progress(tab_id, value))
        view.loadFinished.connect(lambda ok, tab_id=state.id: self._page_load_finished(tab_id, ok))
        view.renderProcessTerminated.connect(
            lambda status, code, tab_id=state.id: self._render_process_terminated(tab_id, status, code)
        )
        page = view.page()
        if hasattr(page, "fullScreenRequested"):
            page.fullScreenRequested.connect(self._handle_fullscreen_request)
        if hasattr(page, "windowCloseRequested"):
            page.windowCloseRequested.connect(lambda tab_id=state.id: self.close_tab(tab_id))

        self.views[state.id] = view
        self._view_tab_ids[id(view)] = state.id
        self.web_stack.addWidget(view)
        group = next((item for item in self.tab_manager.groups if item.id == state.group_id), None)
        index = self.tab_bar.add_tab(
            state.title,
            pinned=state.pinned,
            group=group.name if group else None,
            group_color=group.color if group else None,
            tab_id=state.id,
            make_current=make_current,
        )
        if make_current:
            self.tab_bar.set_current_index(index)
            self.web_stack.setCurrentWidget(view)
        view.setUrl(QUrl(state.url or DEFAULT_NEW_TAB_URL))
        return view

    def close_tab(self, tab_id: str) -> None:
        state = self.tab_manager.get(tab_id)
        if state is None or state.pinned:
            return
        view = self.views.pop(tab_id, None)
        index = self._visual_index(tab_id)
        self.tab_manager.remove(tab_id)
        if index >= 0:
            self.tab_bar.remove_tab(index)
        if view is not None:
            self._view_tab_ids.pop(id(view), None)
            self.web_stack.removeWidget(view)
            view.page().deleteLater()
            view.deleteLater()
        if not self.views and not self._closing:
            self.create_tab()
        elif self.tab_manager.current_id:
            self._activate_tab_id(self.tab_manager.current_id)
        self.snackbar.show_message(
            "Вкладка закрыта", action_text="Вернуть", callback=self._restore_closed_tab
        )

    def _restore_closed_tab(self) -> None:
        state = self.tab_manager.restore_closed()
        if state:
            self._materialize_tab(state, make_current=True)

    def _close_tab_index(self, index: int) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        if metadata:
            self.close_tab(metadata.tab_id)

    def _activate_tab_index(self, index: int) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        if metadata:
            self._activate_tab_id(metadata.tab_id)

    def _activate_tab_id(self, tab_id: str) -> None:
        view = self.views.get(tab_id)
        if view is None:
            return
        self.tab_manager.set_current(tab_id)
        view.page().setLifecycleState(QWebEnginePage.LifecycleState.Active)
        self.web_stack.setCurrentWidget(view)
        index = self._visual_index(tab_id)
        if index >= 0 and self.tab_bar.current_index() != index:
            self.tab_bar.set_current_index(index)
        self._sync_navigation(view)
        view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _visual_index(self, tab_id: str) -> int:
        for index in range(self.tab_bar.count()):
            metadata = self.tab_bar.tab_metadata(index)
            if metadata and metadata.tab_id == tab_id:
                return index
        return -1

    def _page_title_changed(self, tab_id: str, title: str) -> None:
        state = self.tab_manager.update(tab_id, title=title or "Новая вкладка")
        index = self._visual_index(tab_id)
        if state and index >= 0:
            self.tab_bar.update_tab(index, title=state.title)
        if tab_id == self.current_tab_id:
            self.setWindowTitle(f"{state.title if state else title} — Auralis Browser")

    def _page_url_changed(self, tab_id: str, url: QUrl) -> None:
        value = url.toString()
        self.tab_manager.update(tab_id, url=value)
        if tab_id == self.current_tab_id:
            self.navigation.set_url(value)
            self.navigation.set_bookmarked(self.context.bookmarks.is_bookmarked(value) if value else False)

    def _page_icon_changed(self, tab_id: str, icon) -> None:  # noqa: ANN001
        index = self._visual_index(tab_id)
        if index >= 0:
            self.tab_bar.update_tab(index, icon=icon)

    def _page_load_started(self, tab_id: str) -> None:
        index = self._visual_index(tab_id)
        if index >= 0:
            self.tab_bar.update_tab(index, loading=True)
        if tab_id == self.current_tab_id:
            self.navigation.set_loading(True)
            self.navigation.set_progress(4)

    def _page_load_progress(self, tab_id: str, value: int) -> None:
        if tab_id == self.current_tab_id:
            self.navigation.set_progress(value)

    def _page_load_finished(self, tab_id: str, ok: bool) -> None:
        index = self._visual_index(tab_id)
        if index >= 0:
            self.tab_bar.update_tab(index, loading=False)
        if tab_id == self.current_tab_id:
            self.navigation.set_loading(False)
            self._sync_navigation(self.views[tab_id])
        view = self.views.get(tab_id)
        if view is None:
            return
        if view.url().scheme() == "auralis":
            self._apply_theme_to_view(view)
        url = view.url().toString()
        if ok and not self.incognito and url.startswith(("http://", "https://")):
            try:
                self.context.history.add_visit(url, view.title(), transition="link")
            except Exception:
                LOGGER.exception("Could not record history visit")
        if not ok and url.startswith(("http://", "https://")):
            LOGGER.info("Page load failed: %s", url)

    def _sync_navigation(self, view: BrowserView) -> None:
        history = view.history()
        self.navigation.set_navigation_state(can_back=history.canGoBack(), can_forward=history.canGoForward())
        self.navigation.set_url(view.url().toString())
        self.navigation.set_loading(view.isLoading() if hasattr(view, "isLoading") else False)
        url = view.url().toString()
        self.navigation.set_bookmarked(self.context.bookmarks.is_bookmarked(url) if url else False)

    def navigate(self, value: str, *, target_view: BrowserView | None = None) -> None:
        value = " ".join(value.strip().split())
        if not value:
            return
        destination, search_query = self._resolve_destination(value)
        if search_query and not self.incognito:
            try:
                self.context.history.record_search(
                    search_query,
                    search_engine=str(self.settings_values.get("general.search_engine", "google")),
                )
            except Exception:
                LOGGER.exception("Could not record search query")
        view = target_view or self.current_view
        if view is None:
            view = self.create_tab(destination)
        else:
            view.setUrl(QUrl.fromUserInput(destination))

    def _resolve_destination(self, value: str) -> tuple[str, str | None]:
        if value.startswith("auralis://"):
            return value, None
        parsed = QUrl.fromUserInput(value)
        looks_like_url = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value)) or (
            " " not in value and ("." in value or value.startswith(("localhost", "127.0.0.1", "[::1]")))
        )
        if looks_like_url and parsed.isValid():
            destination = parsed.toString()
            if bool(self.settings_values.get("privacy.https_only", False)) and destination.startswith(
                "http://"
            ):
                destination = "https://" + destination[7:]
            return destination, None
        template = SEARCH_ENGINES.get(
            str(self.settings_values.get("general.search_engine", "google")),
            SEARCH_ENGINES["google"],
        )
        return template.format(query=quote_plus(value)), value

    def _go_home(self) -> None:
        homepage = str(self.settings_values.get("general.home_page") or DEFAULT_NEW_TAB_URL)
        self.navigate(homepage)

    def _queue_suggestions(self, _text: str) -> None:
        self._suggestion_timer.start()

    def _refresh_suggestions(self) -> None:
        query = self.navigation.omnibox.text().strip()
        if not query:
            self.navigation.omnibox.set_suggestions([])
            return
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        try:
            for bookmark in self.context.bookmarks.search(query, limit=4):
                if bookmark.url.casefold() not in seen:
                    items.append(
                        {"title": bookmark.title or bookmark.url, "url": bookmark.url, "kind": "bookmark"}
                    )
                    seen.add(bookmark.url.casefold())
            if not self.incognito:
                for suggestion in self.context.history.suggestions(query, limit=6):
                    if suggestion.url.casefold() not in seen:
                        items.append(
                            {
                                "title": suggestion.title or suggestion.url,
                                "url": suggestion.url,
                                "kind": "history",
                            }
                        )
                        seen.add(suggestion.url.casefold())
                for searched in self.context.history.list_searches(query, limit=3):
                    marker = f"search:{searched.query.casefold()}"
                    if marker not in seen:
                        items.append(
                            {"title": f"Искать: {searched.query}", "url": searched.query, "kind": "search"}
                        )
                        seen.add(marker)
        except Exception:
            LOGGER.exception("Could not build omnibox suggestions")
        self.navigation.omnibox.set_suggestions(items[:10])

    def _toggle_bookmark(self) -> None:
        view = self.current_view
        if view is None:
            return
        url = view.url().toString()
        if not url or url.startswith("auralis://"):
            self.snackbar.show_message("Внутренние страницы не добавляются в закладки")
            return
        existing = self.context.bookmarks.find_by_url(url)
        if existing:
            for bookmark in existing:
                self.context.bookmarks.delete(bookmark.id)
            self.navigation.set_bookmarked(False)
            self.snackbar.show_message("Закладка удалена")
            self._refresh_bookmarks_panel()
            return
        folders = [(folder.id, folder.name) for folder in self.context.bookmarks.all_folders()]
        dialog = BookmarkDialog(self, title=view.title(), url=url, folders=folders)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.bookmark_data()
        self.context.bookmarks.add(
            str(data["url"]),
            str(data["title"]),
            folder_id=data.get("folder_id"),
        )
        self.navigation.set_bookmarked(True)
        self.snackbar.show_message("Страница сохранена в закладки")
        self._refresh_bookmarks_panel()

    def _show_side_panel(self, panel: QWidget) -> None:
        if panel is self.bookmarks_panel:
            self._refresh_bookmarks_panel()
        elif panel is self.history_panel:
            self._refresh_history_panel()
        elif panel is self.downloads_panel:
            self._refresh_downloads_panel()
        self.side_stack.setCurrentWidget(panel)
        self.side_stack.show()

    def _refresh_bookmarks_panel(self) -> None:
        folders = {folder.id: folder.name for folder in self.context.bookmarks.all_folders()}
        values = [
            {
                "id": item.id,
                "title": item.title,
                "url": item.url,
                "folder_id": item.folder_id,
                "folder_name": folders.get(item.folder_id, ""),
                "created_at": item.created_at,
            }
            for item in self.context.bookmarks.list_bookmarks(limit=1000)
        ]
        self.bookmarks_panel.set_items(values)
        self._refresh_bookmarks_bar()

    def _refresh_bookmarks_bar(self) -> None:
        self.bookmarks_bar.set_items(
            [
                {"title": item.title, "url": item.url}
                for item in self.context.bookmarks.list_bookmarks(limit=50)
            ]
        )

    def _refresh_history_panel(self) -> None:
        values = [
            {"id": item.id, "title": item.title, "url": item.url, "visited_at": item.visited_at}
            for item in self.context.history.list(limit=500)
        ]
        self.history_panel.set_items(values)

    def _refresh_downloads_panel(self) -> None:
        values = [self._download_record_to_item(item) for item in self.context.downloads.list(limit=500)]
        self.downloads_panel.set_items(values)

    @staticmethod
    def _download_record_to_item(record) -> dict[str, object]:  # noqa: ANN001
        return {
            "id": record.id,
            "file_name": record.suggested_filename or record.file_path.name,
            "url": record.url,
            "path": str(record.file_path),
            "received_bytes": record.received_bytes,
            "total_bytes": max(0, record.total_bytes),
            "speed_bytes": record.bytes_per_second,
            "state": record.status.value,
            "started_at": record.started_at,
            "error": record.error_message,
        }

    def _open_from_panel(self, url: str, new_tab: bool) -> None:
        if new_tab:
            self.create_tab(url)
        else:
            self.navigate(url)

    def _delete_bookmark(self, bookmark_id: object) -> None:
        try:
            self.context.bookmarks.delete(int(bookmark_id))
            self._refresh_bookmarks_panel()
        except (TypeError, ValueError, KeyError):
            self.snackbar.show_message("Не удалось удалить закладку")

    def _edit_bookmark(self, bookmark_id: object) -> None:
        try:
            bookmark = self.context.bookmarks.get(int(bookmark_id))
        except (TypeError, ValueError):
            bookmark = None
        if bookmark is None:
            self.snackbar.show_message("Закладка не найдена")
            return
        folders = [(folder.id, folder.name) for folder in self.context.bookmarks.all_folders()]
        dialog = BookmarkDialog(
            self,
            title=bookmark.title,
            url=bookmark.url,
            folders=folders,
            folder_id=bookmark.folder_id,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.bookmark_data()
        self.context.bookmarks.update(
            bookmark.id,
            title=str(data["title"]),
            url=str(data["url"]),
            folder_id=data.get("folder_id"),
        )
        self._refresh_bookmarks_panel()
        self.snackbar.show_message("Закладка обновлена")

    def _new_bookmark_folder(self) -> None:
        name, accepted = QInputDialog.getText(self, "Новая папка", "Название папки")
        if accepted and name.strip():
            self.context.bookmarks.create_folder(name)
            self._refresh_bookmarks_panel()

    def _delete_history_entry(self, entry_id: object) -> None:
        try:
            self.context.history.delete(int(entry_id))
            self._refresh_history_panel()
        except (TypeError, ValueError):
            self.snackbar.show_message("Не удалось удалить запись")

    def _import_bookmarks(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт закладок", "", "Auralis JSON (*.json)")
        if not path:
            return
        try:
            result = self.context.bookmarks.import_json(path)
            self.snackbar.show_message(f"Импортировано: {result.bookmarks_imported} закладок")
            self._refresh_bookmarks_panel()
        except Exception as error:
            LOGGER.exception("Bookmark import failed")
            QMessageBox.warning(self, "Импорт закладок", str(error))

    def _export_bookmarks(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт закладок", "auralis-bookmarks.json", "JSON (*.json)"
        )
        if path:
            self.context.bookmarks.export_json(path)
            self.snackbar.show_message("Закладки экспортированы")

    def _load_persisted_downloads(self) -> None:
        try:
            self.context.downloads.mark_active_interrupted()
        except Exception:
            LOGGER.exception("Could not mark previous downloads as interrupted")

    def _on_download_created(self, download: BrowserDownload) -> None:
        directory = Path(
            str(self.settings_values.get("downloads.directory") or self.context.profile.paths.downloads)
        )
        filename = download.filename or "download"
        if bool(self.settings_values.get("downloads.ask_location", False)):
            chosen, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", str(directory / filename))
            if not chosen:
                download.cancel()
                return
            target = Path(chosen)
            directory, filename = target.parent, target.name
        try:
            download.accept(directory, filename)
            request = download.request
            record = self.context.downloads.create(
                request.url().toString(),
                directory / filename,
                guid=download.id,
                suggested_filename=filename,
                mime_type=request.mimeType() or "application/octet-stream",
                total_bytes=max(-1, download.total),
                status=DownloadStatus.IN_PROGRESS,
            )
        except Exception as error:
            LOGGER.exception("Could not start download")
            download.cancel()
            self.snackbar.show_message(f"Не удалось начать загрузку: {error}")
            return
        self._download_db_ids[download.id] = record.id
        self._downloads_by_key[record.id] = download
        self._downloads_by_key[download.id] = download
        download.changed.connect(lambda item=download: self._download_changed(item))
        download.finished.connect(lambda item=download: self._download_finished(item))
        self.downloads_panel.update_download(record.id, **self._download_record_to_item(record))
        self.snackbar.show_message(
            "Загрузка началась",
            action_text="Показать",
            callback=lambda: self._show_side_panel(self.downloads_panel),
        )

    def _download_changed(self, download: BrowserDownload, *, force: bool = False) -> None:
        database_id = self._download_db_ids.get(download.id)
        if database_id is None:
            return
        now = time.monotonic()
        last = self._download_write_times.get(download.id, 0.0)
        if not force and now - last < 0.4:
            return
        self._download_write_times[download.id] = now
        try:
            record = self.context.downloads.update_progress(
                database_id,
                max(0, download.received),
                total_bytes=max(-1, download.total),
                bytes_per_second=max(0.0, download.speed),
            )
            self.downloads_panel.update_download(database_id, **self._download_record_to_item(record))
        except Exception:
            LOGGER.exception("Could not persist download progress")

    def _download_finished(self, download: BrowserDownload) -> None:
        self._download_changed(download, force=True)
        database_id = self._download_db_ids.get(download.id)
        if database_id is None:
            return
        state = download.state.lower()
        status = (
            DownloadStatus.COMPLETED
            if "completed" in state
            else DownloadStatus.CANCELLED
            if "cancelled" in state
            else DownloadStatus.FAILED
        )
        try:
            record = self.context.downloads.set_status(database_id, status)
            self.downloads_panel.update_download(database_id, **self._download_record_to_item(record))
        except Exception:
            LOGGER.exception("Could not finalize download record")
        if status is DownloadStatus.COMPLETED and bool(
            self.settings_values.get("downloads.notifications", True)
        ):
            self.snackbar.show_message(
                f"Файл «{download.filename}» загружен",
                action_text="Открыть",
                callback=download.open_file,
                timeout=6500,
            )

    def _download_for(self, key: object) -> BrowserDownload | None:
        if key in self._downloads_by_key:
            return self._downloads_by_key[key]
        try:
            return self._downloads_by_key.get(int(key))
        except (TypeError, ValueError):
            return None

    def _pause_download(self, key: object) -> None:
        download = self._download_for(key)
        if download:
            download.pause()
        try:
            self.context.downloads.pause(int(key))
            self._refresh_downloads_panel()
        except (TypeError, ValueError, KeyError):
            pass

    def _resume_download(self, key: object) -> None:
        download = self._download_for(key)
        if download:
            download.resume()
        try:
            self.context.downloads.resume(int(key))
            self._refresh_downloads_panel()
        except (TypeError, ValueError, KeyError):
            self.snackbar.show_message("Эту загрузку нельзя продолжить после перезапуска")

    def _cancel_download(self, key: object) -> None:
        download = self._download_for(key)
        if download:
            download.cancel()
        try:
            self.context.downloads.cancel(int(key))
            self._refresh_downloads_panel()
        except (TypeError, ValueError, KeyError):
            pass

    def _open_download(self, key: object) -> None:
        record = self.context.downloads.get(int(key))
        if record and record.file_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.file_path)))
        else:
            self.snackbar.show_message("Файл больше не существует")

    def _show_download_in_folder(self, key: object) -> None:
        record = self.context.downloads.get(int(key))
        if record:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.file_path.parent)))

    def _remove_download_record(self, key: object) -> None:
        try:
            self.context.downloads.delete(int(key))
            self._refresh_downloads_panel()
        except (TypeError, ValueError, KeyError):
            self.snackbar.show_message("Активную загрузку нельзя удалить")

    def _clear_completed_downloads(self) -> None:
        self.context.downloads.clear_finished()
        self._refresh_downloads_panel()

    def _open_downloads_folder(self) -> None:
        directory = Path(
            str(self.settings_values.get("downloads.directory") or self.context.profile.paths.downloads)
        )
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    @staticmethod
    def _normalize_permission(feature: str) -> PermissionType | None:
        compact = re.sub(r"[^a-z]", "", feature.lower())
        mapping = {
            "notifications": PermissionType.NOTIFICATIONS,
            "notification": PermissionType.NOTIFICATIONS,
            "geolocation": PermissionType.GEOLOCATION,
            "mediavideocapture": PermissionType.CAMERA,
            "camera": PermissionType.CAMERA,
            "mediaaudiocapture": PermissionType.MICROPHONE,
            "microphone": PermissionType.MICROPHONE,
            "clipboardreadwrite": PermissionType.CLIPBOARD_READ,
            "clipboardread": PermissionType.CLIPBOARD_READ,
            "midi": PermissionType.MIDI,
            "midisysex": PermissionType.MIDI,
        }
        return mapping.get(compact)

    def _on_permission_requested(self, request) -> None:  # noqa: ANN001
        permission = self._normalize_permission(request.feature)
        if permission is None:
            request.deny()
            return
        try:
            stored = self.context.security.permissions.get(request.origin, permission)
        except ValueError:
            request.deny()
            return
        if stored is StoredPermissionDecision.ALLOW:
            request.grant()
            return
        if stored is StoredPermissionDecision.BLOCK:
            request.deny()
            return
        dialog = PermissionDialog(request.origin, permission.value, self)
        dialog.exec()
        if dialog.decision is PermissionDecision.ALLOW_ONCE:
            request.grant()
        elif dialog.decision is PermissionDecision.ALLOW_ALWAYS:
            self.context.security.permissions.set(request.origin, permission, StoredPermissionDecision.ALLOW)
            request.grant()
        else:
            self.context.security.permissions.set(request.origin, permission, StoredPermissionDecision.BLOCK)
            request.deny()

    def _on_notification(self, notification) -> None:  # noqa: ANN001
        title = notification.title() if hasattr(notification, "title") else "Уведомление сайта"
        message = notification.message() if hasattr(notification, "message") else ""
        self.snackbar.show_message(f"{title}: {message}"[:220], timeout=6000)

    def _show_settings(self, section: str = "general") -> None:
        dialog = SettingsDialog(self)
        dialog.load_settings(self.settings_values)
        dialog.show_section(section)
        dialog.settingChanged.connect(self._setting_changed)
        dialog.clearBrowsingDataRequested.connect(self._show_clear_data)
        dialog.clearCacheRequested.connect(self._clear_cache)
        dialog.managePermissionsRequested.connect(self._show_permissions)
        dialog.extensionsRequested.connect(self._show_extensions)
        dialog.profilesRequested.connect(self._show_profiles)
        dialog.syncRequested.connect(self._show_sync_information)
        self._settings_dialog = dialog
        dialog.exec()
        self._settings_dialog = None

    def _setting_changed(self, key: str, value: object) -> None:
        self.settings_values[key] = value
        self.context.settings.set(key, value, namespace=SETTINGS_NAMESPACE)
        if key == "appearance.theme":
            self.context.theme.set_mode(str(value))
        elif key == "appearance.accent":
            self.context.theme.set_accent(str(value))
        elif key == "appearance.density":
            self.context.theme.set_density(str(value))
        elif key == "appearance.ui_scale":
            self.context.theme.set_scale(int(value))
        elif key == "appearance.show_bookmarks_bar":
            self.bookmarks_bar.setVisible(bool(value))
            self._refresh_bookmarks_bar()
        elif key == "general.search_engine":
            template = SEARCH_ENGINES.get(str(value), SEARCH_ENGINES["google"])
            self.context.engine.set_search_template(template)
        elif key == "privacy.tracking_protection":
            self.context.adblocker.set_enabled(str(value) != "off")
        elif key == "privacy.third_party_cookies":
            self.context.engine.set_cookie_policy(self.context.profile.id, allow_third_party=not bool(value))
        elif key == "privacy.do_not_track":
            self.context.engine.set_do_not_track(self.context.profile.id, bool(value))
        elif key == "performance.preload_pages":
            self.context.engine.set_page_preloading(self.context.profile.id, bool(value))
        elif key == "performance.memory_saver":
            self._apply_memory_saver()
        elif key == "performance.hardware_acceleration":
            self.snackbar.show_message(
                "Настройка ускорения применится после перезапуска", timeout=5000
            )

    def _apply_web_theme(self) -> None:
        for view in self.views.values():
            if view.url().scheme() == "auralis":
                self._apply_theme_to_view(view)

    def _apply_theme_to_view(self, view: BrowserView) -> None:
        palette = self.context.theme.palette
        roles = {
            "--primary": palette.primary,
            "--on-primary": palette.on_primary,
            "--primary-container": palette.primary_container,
            "--on-primary-container": palette.on_primary_container,
            "--secondary-container": palette.secondary_container,
            "--surface": palette.surface,
            "--surface-container": palette.surface_container,
            "--surface-high": palette.surface_container_high,
            "--on-surface": palette.on_surface,
            "--on-variant": palette.on_surface_variant,
            "--outline": palette.outline,
        }
        statements = "".join(
            f"document.documentElement.style.setProperty({name!r}, {value!r});"
            for name, value in roles.items()
        )
        mode = "dark" if palette.is_dark else "light"
        view.page().runJavaScript(f"document.documentElement.dataset.theme={mode!r};{statements}")

    def _apply_memory_saver(self) -> None:
        enabled = bool(self.settings_values.get("performance.memory_saver", True))
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        for tab_id, view in self.views.items():
            if tab_id == self.current_tab_id or not enabled:
                lifecycle = QWebEnginePage.LifecycleState.Active
            else:
                tab = self.tab_manager.get(tab_id)
                try:
                    last_active = datetime.fromisoformat(tab.last_active_at) if tab else cutoff
                except ValueError:
                    last_active = cutoff
                lifecycle = (
                    QWebEnginePage.LifecycleState.Frozen
                    if last_active <= cutoff
                    else QWebEnginePage.LifecycleState.Active
                )
            if view.page().lifecycleState() != lifecycle:
                view.page().setLifecycleState(lifecycle)

    def _show_clear_data(self) -> None:
        dialog = ClearBrowsingDataDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selection = dialog.selection()
        now = datetime.now(UTC)
        cutoff = {
            "hour": now - timedelta(hours=1),
            "day": now - timedelta(days=1),
            "week": now - timedelta(days=7),
            "month": now - timedelta(days=28),
        }.get(selection.time_range)
        try:
            if selection.history:
                if cutoff is None:
                    self.context.history.clear()
                    self.context.history.clear_search_history()
                else:
                    self.context.history.delete_between(cutoff, now)
            if selection.downloads:
                self.context.downloads.clear_finished()
            if selection.cookies or selection.cache:
                if selection.cookies:
                    self.context.engine.clear_site_data(self.context.profile.id, cookies=True)
                elif selection.cache:
                    self.context.engine.clear_cache(self.context.profile.id)
            if selection.permissions:
                self.context.security.permissions.clear()
        except Exception as error:
            LOGGER.exception("Could not clear browsing data")
            QMessageBox.warning(self, "Очистка данных", str(error))
            return
        self._refresh_history_panel()
        self._refresh_downloads_panel()
        self.snackbar.show_message("Выбранные данные удалены")

    def _clear_cache(self) -> None:
        self.context.engine.clear_cache(self.context.profile.id)
        self.snackbar.show_message("Кеш очищается в фоновом режиме")

    def _show_permissions(self) -> None:
        permissions = self.context.security.permissions.list_all()
        if permissions:
            text = "\n".join(
                f"{item.origin} — {item.permission.value}: {item.decision.value}" for item in permissions[:50]
            )
        else:
            text = "Сохранённых разрешений пока нет."
        box = QMessageBox(self)
        box.setWindowTitle("Разрешения сайтов")
        box.setText(text)
        reset = box.addButton("Сбросить все", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Готово", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is reset:
            self.context.security.permissions.clear()
            self.snackbar.show_message("Разрешения сброшены")

    def _show_extensions(self) -> None:
        try:
            extensions = self.context.extensions.refresh()
        except Exception:
            LOGGER.exception("Could not scan extensions")
            extensions = ()
        details = [
            f"• {item.manifest.name} {item.manifest.version} — {'включено' if item.enabled else 'выключено'}"
            for item in extensions
        ]
        text = (
            "\n".join(details)
            if details
            else (
                "Распакованные расширения ещё не установлены. Auralis проверяет manifest.json и поддерживает "
                "content scripts через подготовленный runtime-адаптер."
            )
        )
        box = QMessageBox(self)
        box.setWindowTitle("Расширения")
        box.setText(text)
        install = box.addButton("Загрузить распакованное", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Закрыть", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is install:
            directory = QFileDialog.getExistingDirectory(self, "Папка расширения")
            if directory:
                try:
                    extension = self.context.extensions.install_unpacked(directory)
                    self.snackbar.show_message(f"Расширение «{extension.manifest.name}» установлено")
                except Exception as error:
                    QMessageBox.warning(self, "Расширения", str(error))

    def _show_profiles(self) -> None:
        menu = QMenu(self)
        menu.setTitle("Профили")
        for profile in self.context.profile_manager.list_profiles():
            action = menu.addAction(("✓  " if profile.id == self.context.profile.id else "") + profile.name)
            action.triggered.connect(
                lambda _checked=False, profile_id=profile.id: self.profileWindowRequested.emit(
                    profile_id, False
                )
            )
        menu.addSeparator()
        avatar = menu.addAction("Изменить аватар…")
        avatar.triggered.connect(self._change_profile_avatar)
        rename = menu.addAction("Переименовать профиль…")
        rename.triggered.connect(self._rename_profile)
        create = menu.addAction("Создать профиль…")
        create.triggered.connect(self._create_profile)
        menu.exec(self.mapToGlobal(self.rect().center()))

    def _create_profile(self) -> None:
        name, accepted = QInputDialog.getText(self, "Новый профиль", "Имя профиля")
        if not accepted or not name.strip():
            return
        try:
            profile = self.context.profile_manager.create_profile(name, activate=False)
        except Exception as error:
            QMessageBox.warning(self, "Новый профиль", str(error))
            return
        self.profileWindowRequested.emit(profile.id, False)

    def _change_profile_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Аватар профиля",
            "",
            "Изображения (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not path:
            return
        updated = self.context.profile_manager.update_profile(
            self.context.profile.id, avatar_path=path
        )
        self.context.profile = updated
        self.navigation.set_profile(updated.name, str(updated.avatar_path))

    def _rename_profile(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            "Имя профиля",
            "Имя",
            text=self.context.profile.name,
        )
        if not accepted or not name.strip():
            return
        updated = self.context.profile_manager.update_profile(
            self.context.profile.id, name=name
        )
        self.context.profile = updated
        self.navigation.set_profile(
            updated.name,
            str(updated.avatar_path) if updated.avatar_path else None,
        )

    def _show_sync_information(self) -> None:
        QMessageBox.information(
            self,
            "Синхронизация",
            "Абстрактный SyncBackend уже отделён от локальных данных. Подключите серверный backend и учётную запись в services/sync.py.",
        )

    def _show_site_information(self) -> None:
        view = self.current_view
        if view is None:
            return
        url = view.url()
        origin = f"{url.scheme()}://{url.host()}" if url.host() else url.toString()
        permission_count = sum(
            1 for item in self.context.security.permissions.list_all() if item.origin == origin
        )
        dialog = SiteInformationDialog(
            {
                "origin": origin or "Внутренняя страница",
                "secure": url.scheme() in {"https", "auralis"},
                "permissions_count": permission_count,
            },
            self,
        )
        dialog.managePermissionsRequested.connect(lambda _origin: self._show_permissions())
        dialog.clearSiteDataRequested.connect(lambda _origin: self._clear_site_data())
        dialog.exec()

    def _clear_site_data(self) -> None:
        self.context.engine.clear_site_data(self.context.profile.id, cookies=True)
        self.snackbar.show_message("Cookies и кеш профиля очищены")

    def _pin_tab(self, index: int, pinned: bool) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        if not metadata:
            return
        self.tab_manager.update(metadata.tab_id, pinned=pinned)
        new_index = self.tab_bar.update_tab(index, pinned=pinned)
        self.tab_bar.set_current_index(new_index)

    def _duplicate_tab_index(self, index: int) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        state = self.tab_manager.get(metadata.tab_id) if metadata else None
        if state:
            self.create_tab(state.url, group_id=state.group_id)

    def _group_tab(self, index: int, requested: str) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        if not metadata:
            return
        if requested == "":
            self.tab_manager.update(metadata.tab_id, group_id=None)
            self.tab_bar.update_tab(index, group="", group_color="")
            return
        if requested == "__new__":
            name, accepted = QInputDialog.getText(self, "Группа вкладок", "Название")
            if not accepted or not name.strip():
                return
            group = self.tab_manager.create_group(name)
        else:
            group = next((item for item in self.tab_manager.groups if item.id == requested), None)
            if group is None:
                return
        self.tab_manager.update(metadata.tab_id, group_id=group.id)
        self.tab_bar.update_tab(index, group=group.name, group_color=group.color)

    def _show_tab_preview(self, index: int, anchor: QPoint) -> None:
        metadata = self.tab_bar.tab_metadata(index)
        if not metadata:
            return
        view = self.views.get(metadata.tab_id)
        state = self.tab_manager.get(metadata.tab_id)
        pixmap = view.grab() if view is not None else QPixmap()
        self.preview.show_preview(
            pixmap, state.title if state else metadata.title, state.url if state else "", anchor
        )

    def _show_main_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.setObjectName("mainBrowserMenu")
        self._menu_action(menu, "Новая вкладка", lambda: self.create_tab(), "Ctrl+T")
        self._menu_action(
            menu,
            "Новое окно",
            lambda: self.profileWindowRequested.emit(self.context.profile.id, False),
            "Ctrl+N",
        )
        self._menu_action(
            menu,
            "Приватное окно",
            lambda: self.profileWindowRequested.emit(self.context.profile.id, True),
            "Ctrl+Shift+N",
        )
        menu.addSeparator()
        self._menu_action(
            menu, "Закладки", lambda: self._show_side_panel(self.bookmarks_panel), "Ctrl+Shift+B"
        )
        self._menu_action(menu, "История", lambda: self._show_side_panel(self.history_panel), "Ctrl+H")
        self._menu_action(menu, "Загрузки", lambda: self._show_side_panel(self.downloads_panel), "Ctrl+J")
        menu.addSeparator()

        zoom_menu = menu.addMenu(
            f"Масштаб · {round((self.current_view.zoomFactor() if self.current_view else 1) * 100)}%"
        )
        self._menu_action(zoom_menu, "Увеличить", lambda: self._change_zoom(0.1), "Ctrl++")
        self._menu_action(zoom_menu, "Уменьшить", lambda: self._change_zoom(-0.1), "Ctrl+-")
        self._menu_action(zoom_menu, "Сбросить", self._reset_zoom, "Ctrl+0")
        self._menu_action(menu, "Найти на странице", self._open_find_bar, "Ctrl+F")
        self._menu_action(menu, "Сохранить страницу в PDF", self._save_pdf)
        tools = menu.addMenu("Инструменты страницы")
        self._menu_action(tools, "Режим чтения", self._reader_mode)
        self._menu_action(tools, "Перевести страницу", self._translate_page)
        self._menu_action(tools, "QR-код страницы", self._show_qr_code)
        self._menu_action(tools, "Поделиться", self._share_page)
        self._menu_action(tools, "Инструменты разработчика", self._open_devtools, "F12")
        menu.addSeparator()

        adblock = menu.addMenu(f"Блокировка рекламы · {self.context.adblocker.blocked_count}")
        enabled = adblock.addAction("Включена")
        enabled.setCheckable(True)
        enabled.setChecked(self.context.adblocker.enabled)
        enabled.toggled.connect(self.context.adblocker.set_enabled)
        update_filters = adblock.addAction("Обновить EasyList")
        update_filters.triggered.connect(self._update_easylist)
        host = self.current_view.url().host() if self.current_view else ""
        if host:
            whitelisted = host.casefold() in self.context.adblocker.whitelist
            whitelist_action = adblock.addAction(
                "Убрать сайт из исключений" if whitelisted else "Не блокировать на этом сайте"
            )
            whitelist_action.triggered.connect(lambda: self._toggle_adblock_whitelist(host, whitelisted))
        self._menu_action(menu, "Настройки", self._show_settings, "Ctrl+,")
        self._menu_action(menu, f"Профиль: {self.context.profile.name}", self._show_profiles)
        menu.addSeparator()
        self._menu_action(menu, "Выход", QApplication.instance().quit)
        menu.exec(position)

    @staticmethod
    def _menu_action(menu: QMenu, text: str, callback, shortcut: str = "") -> QAction:  # noqa: ANN001
        action = menu.addAction(text)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutVisibleInContextMenu(True)
        action.triggered.connect(callback)
        return action

    def _toggle_adblock_whitelist(self, host: str, currently_whitelisted: bool) -> None:
        if currently_whitelisted:
            self.context.adblocker.remove_whitelist(host)
            message = "Блокировка на сайте включена"
        else:
            self.context.adblocker.whitelist_domain(host)
            message = "Сайт добавлен в исключения"
        self.snackbar.show_message(
            message, action_text="Обновить", callback=lambda: self.current_view and self.current_view.reload()
        )

    def _update_easylist(self) -> None:
        if self.incognito:
            self.snackbar.show_message("Обновите фильтры в обычном окне")
            return
        self.snackbar.show_message("Загрузка EasyList…", timeout=2500)
        destination = self.context.profile.paths.root / "filters" / "easylist.txt"
        task = asyncio.create_task(update_filter_subscription(self.context.adblocker, destination))
        self._background_tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                result = completed.result()
            except Exception as error:
                LOGGER.exception("EasyList update failed")
                self.snackbar.show_message(f"Не удалось обновить EasyList: {error}", timeout=6500)
                return
            self.snackbar.show_message(f"EasyList обновлён · {result.rules_loaded:,} правил", timeout=5000)

        task.add_done_callback(finished)

    def _change_zoom(self, delta: float) -> None:
        view = self.current_view
        if view is None:
            return
        value = max(0.25, min(5.0, view.zoomFactor() + delta))
        view.setZoomFactor(value)
        if self.current_tab_id:
            self.tab_manager.update(self.current_tab_id, zoom=value)
        self.snackbar.show_message(f"Масштаб: {round(value * 100)}%", timeout=1600)

    def _reset_zoom(self) -> None:
        view = self.current_view
        if view:
            view.setZoomFactor(1.0)
            if self.current_tab_id:
                self.tab_manager.update(self.current_tab_id, zoom=1.0)

    def _open_find_bar(self) -> None:
        self.find_bar.open()

    def _find_text(self, text: str, backward: bool) -> None:
        view = self.current_view
        if view is None:
            return
        flag = QWebEnginePage.FindFlag.FindBackward if backward else QWebEnginePage.FindFlag(0)

        def result_ready(result) -> None:  # noqa: ANN001
            self.find_bar.set_result(result.activeMatch(), result.numberOfMatches())

        view.page().findText(text, flag, result_ready)

    def _save_pdf(self) -> None:
        view = self.current_view
        if view is None:
            return
        suggested = re.sub(r"[<>:\"/\\|?*]", "_", view.title() or "page") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить в PDF", suggested, "PDF (*.pdf)")
        if path:
            view.page().printToPdf(path)
            self.snackbar.show_message("Создание PDF запущено")

    def _reader_mode(self) -> None:
        source = self.current_view
        if source is None or not source.url().toString().startswith(("http://", "https://")):
            self.snackbar.show_message("Режим чтения доступен для веб-страниц")
            return
        script = """
        (() => {
          const article = document.querySelector('article, main, [role="main"]') || document.body;
          return {title: document.title, text: (article.innerText || '').slice(0, 400000), url: location.href};
        })()
        """

        def render(result: object) -> None:
            if not isinstance(result, dict) or not str(result.get("text", "")).strip():
                self.snackbar.show_message("Не удалось выделить текст статьи")
                return
            title = html.escape(str(result.get("title") or "Режим чтения"))
            body = "".join(
                f"<p>{html.escape(paragraph)}</p>"
                for paragraph in str(result["text"]).split("\n")
                if paragraph.strip()
            )
            document = f"""<!doctype html><meta charset='utf-8'><meta name='color-scheme' content='light dark'>
            <title>{title}</title><style>body{{max-width:760px;margin:8vh auto;padding:0 28px;font:19px/1.75 Georgia,serif;background:#fdf8ff;color:#1d1b20}}h1{{font:650 42px/1.15 system-ui;margin-bottom:36px}}p{{margin:0 0 1.1em}}@media(prefers-color-scheme:dark){{body{{background:#141218;color:#e6e0e9}}}}</style><h1>{title}</h1>{body}"""
            reader = self.create_tab("about:blank")
            reader.setHtml(document, QUrl(str(result.get("url", ""))))

        source.page().runJavaScript(script, render)

    def _translate_page(self) -> None:
        view = self.current_view
        if view is None:
            return
        url = view.url().toString()
        if url.startswith(("http://", "https://")):
            self.create_tab(f"https://translate.google.com/translate?sl=auto&tl=ru&u={quote_plus(url)}")

    def _show_qr_code(self) -> None:
        view = self.current_view
        if view is None:
            return
        image = qrcode.make(view.url().toString())
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pixmap = QPixmap.fromImage(QImage.fromData(buffer.getvalue()))
        dialog = QDialog(self)
        dialog.setWindowTitle("QR-код страницы")
        label = QLabel(dialog)
        label.setPixmap(
            pixmap.scaled(
                320, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        url_label = QLabel(view.url().toString(), dialog)
        url_label.setWordWrap(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(label)
        layout.addWidget(url_label)
        dialog.exec()

    def _share_page(self) -> None:
        view = self.current_view
        if view:
            QApplication.clipboard().setText(f"{view.title()}\n{view.url().toString()}")
            self.snackbar.show_message("Название и адрес скопированы")

    def _open_devtools(self) -> None:
        view = self.current_view
        if view is None:
            return
        window = QMainWindow(self)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.setWindowTitle(f"DevTools — {view.title()}")
        dev_view = QWebEngineView(window)
        dev_page = QWebEnginePage(view.page().profile(), dev_view)
        dev_view.setPage(dev_page)
        window.setCentralWidget(dev_view)
        window.resize(1050, 720)
        view.page().setDevToolsPage(dev_page)
        window.destroyed.connect(
            lambda: self._devtools_windows.remove(window) if window in self._devtools_windows else None
        )
        self._devtools_windows.append(window)
        window.show()

    def _handle_fullscreen_request(self, request) -> None:  # noqa: ANN001
        request.accept()
        self._set_browser_fullscreen(bool(request.toggleOn()))

    def _set_browser_fullscreen(self, enabled: bool) -> None:
        self._full_screen = enabled
        self.tab_bar.setVisible(not enabled)
        self.navigation.setVisible(not enabled)
        self.find_bar.setVisible(not enabled and self.find_bar.isVisible())
        self.side_stack.setVisible(not enabled and self.side_stack.isVisible())
        if enabled:
            self.showFullScreen()
            self.snackbar.show_message("Полноэкранный режим · Esc для выхода", timeout=2600)
        else:
            self.showNormal()

    def _render_process_terminated(self, tab_id: str, status: object, code: int) -> None:
        if "NormalTermination" in str(status):
            return
        LOGGER.error("Web render process terminated for tab %s: %s (%s)", tab_id, status, code)
        if tab_id == self.current_tab_id:
            self.snackbar.show_message(
                "Процесс страницы завершился",
                action_text="Перезагрузить",
                callback=lambda: self.views.get(tab_id) and self.views[tab_id].reload(),
            )

    def _install_shortcuts(self) -> None:
        self._shortcuts: list[QShortcut] = []

        def add(sequence: str, callback) -> None:  # noqa: ANN001
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        add("Ctrl+T", lambda: self.create_tab())
        add("Ctrl+W", self._close_current_tab)
        add("Ctrl+Shift+T", self._restore_closed_tab)
        add("Ctrl+L", self.navigation.omnibox.focus_and_select)
        add("Ctrl+R", lambda: self.current_view and self.current_view.reload())
        add("F5", lambda: self.current_view and self.current_view.reload())
        add("Alt+Left", lambda: self.current_view and self.current_view.back())
        add("Alt+Right", lambda: self.current_view and self.current_view.forward())
        add("Ctrl+D", self._toggle_bookmark)
        add("Ctrl+H", lambda: self._show_side_panel(self.history_panel))
        add("Ctrl+J", lambda: self._show_side_panel(self.downloads_panel))
        add("Ctrl+Shift+B", lambda: self._show_side_panel(self.bookmarks_panel))
        add("Ctrl+,", self._show_settings)
        add("Ctrl+F", self._open_find_bar)
        add("Ctrl++", lambda: self._change_zoom(0.1))
        add("Ctrl+=", lambda: self._change_zoom(0.1))
        add("Ctrl+-", lambda: self._change_zoom(-0.1))
        add("Ctrl+0", self._reset_zoom)
        add("Ctrl+Tab", lambda: self._cycle_tabs(1))
        add("Ctrl+Shift+Tab", lambda: self._cycle_tabs(-1))
        add("Ctrl+N", lambda: self.profileWindowRequested.emit(self.context.profile.id, False))
        add("Ctrl+Shift+N", lambda: self.profileWindowRequested.emit(self.context.profile.id, True))
        add("F11", lambda: self._set_browser_fullscreen(not self._full_screen))
        add("F12", self._open_devtools)
        add("Escape", self._escape_action)
        for number in range(1, 9):
            add(f"Ctrl+{number}", lambda value=number: self._select_tab_number(value - 1))
        add("Ctrl+9", lambda: self._select_tab_number(self.tab_bar.count() - 1))

    def _close_current_tab(self) -> None:
        if self.current_tab_id:
            state = self.tab_manager.get(self.current_tab_id)
            if state and state.pinned:
                self.snackbar.show_message("Сначала открепите вкладку")
            else:
                self.close_tab(self.current_tab_id)

    def _cycle_tabs(self, step: int) -> None:
        count = self.tab_bar.count()
        if count:
            self.tab_bar.set_current_index((self.tab_bar.current_index() + step) % count)

    def _select_tab_number(self, index: int) -> None:
        if 0 <= index < self.tab_bar.count():
            self.tab_bar.set_current_index(index)

    def _escape_action(self) -> None:
        if self._full_screen:
            self._set_browser_fullscreen(False)
        elif self.find_bar.isVisible():
            self.find_bar.close_bar()
        elif self.current_view:
            self.current_view.stop()

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self.snackbar.parent_resized()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        for task in self._background_tasks:
            task.cancel()
        if self.incognito:
            try:
                self.tab_manager.session_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Could not remove private session file")
        else:
            self.tab_manager.save_session()
        for view in self.views.values():
            view.stop()
        event.accept()


__all__ = ["BrowserContext", "BrowserWindow"]
