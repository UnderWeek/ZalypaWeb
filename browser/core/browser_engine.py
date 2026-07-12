"""Qt WebEngine integration for Auralis Browser.

This module owns Chromium profiles and translates low-level WebEngine events to
small Qt signals consumed by the main window.  No application widget is created
until :class:`BrowserEngine` is instantiated after ``QApplication``.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QStandardPaths,
    QUrl,
    QUrlQuery,
    Signal,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

LOGGER = logging.getLogger(__name__)
INTERNAL_SCHEME = b"auralis"
_SCHEME_REGISTERED = False


def register_internal_scheme() -> None:
    """Register ``auralis://`` before ``QApplication`` is constructed."""

    global _SCHEME_REGISTERED
    if _SCHEME_REGISTERED:
        return
    scheme = QWebEngineUrlScheme(INTERNAL_SCHEME)
    # Internal pages use a simple host (``auralis://newtab``).  Declaring a
    # port-based syntax with port 0 makes recent Chromium builds terminate the
    # renderer while validating the origin, so the stricter Host syntax is
    # intentional here.
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.CorsEnabled
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    _SCHEME_REGISTERED = True


@dataclass(slots=True)
class ProfilePaths:
    """Filesystem locations used by one Chromium profile."""

    root: Path
    storage: Path
    cache: Path
    downloads: Path

    @classmethod
    def beneath(cls, root: str | Path, downloads: str | Path | None = None) -> ProfilePaths:
        base = Path(root)
        default_downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        return cls(
            root=base,
            storage=base / "webengine" / "storage",
            cache=base / "webengine" / "cache",
            downloads=Path(downloads or default_downloads or base / "downloads"),
        )

    def ensure(self) -> None:
        for path in (self.root, self.storage, self.cache, self.downloads):
            path.mkdir(parents=True, exist_ok=True)


class InternalSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves trusted bundled pages without running a local HTTP server."""

    def __init__(
        self,
        resource_root: str | Path,
        search_url: Callable[[str], str],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._resource_root = Path(resource_root).resolve()
        self._search_url = search_url

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802 - Qt API
        url = job.requestUrl()
        host = url.host().lower()
        if host == "navigate":
            raw = QUrlQuery(url).queryItemValue("q", QUrl.ComponentFormattingOption.FullyDecoded)
            destination = self._search_url(raw)
            job.redirect(QUrl.fromUserInput(destination))
            return
        page_name = {
            "newtab": "newtab.html",
            "start": "newtab.html",
            "welcome": "newtab.html",
            "offline": "offline.html",
        }.get(host, "not_found.html")
        path = (self._resource_root / page_name).resolve()
        if self._resource_root not in path.parents or not path.is_file():
            self._reply(job, self._fallback_page(host), b"text/html; charset=utf-8")
            return
        try:
            data = path.read_bytes()
        except OSError:
            LOGGER.exception("Unable to serve internal page %s", path)
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._reply(job, data, f"{mime}; charset=utf-8".encode("ascii"))

    @staticmethod
    def _reply(job: QWebEngineUrlRequestJob, data: bytes, mime: bytes) -> None:
        buffer = QBuffer(job)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, buffer)

    @staticmethod
    def _fallback_page(name: str) -> bytes:
        safe_name = html.escape(name or "page")
        return (
            "<!doctype html><meta charset='utf-8'><style>body{font:16px system-ui;"
            "background:#f7f2fa;color:#1d1b20;padding:10vh 12vw}</style>"
            f"<h1>Страница недоступна</h1><p>Auralis не нашёл: {safe_name}</p>"
        ).encode()


class RequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Applies the built-in blocker before Chromium starts a request."""

    blocked = Signal(str, str)

    def __init__(
        self,
        adblocker: Any | None = None,
        *,
        do_not_track: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.adblocker = adblocker
        self.do_not_track = do_not_track

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802 - Qt API
        if self.do_not_track:
            info.setHttpHeader(b"DNT", b"1")
        if self.adblocker is None:
            return
        url = info.requestUrl().toString()
        first_party = info.firstPartyUrl().toString()
        resource_type = getattr(info.resourceType(), "name", str(info.resourceType()))
        try:
            should_block = bool(self.adblocker.should_block(url, first_party, resource_type))
        except TypeError:
            should_block = bool(self.adblocker.should_block(url, first_party))
        except Exception:
            LOGGER.exception("Ad blocker failed while evaluating %s", url)
            return
        if should_block:
            info.block(True)
            self.blocked.emit(url, first_party)


class PermissionRequest(QObject):
    """Version-neutral wrapper around WebEngine permission APIs."""

    resolved = Signal(bool)

    def __init__(
        self,
        origin: str,
        feature: str,
        accept: Callable[[], None],
        reject: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.origin = origin
        self.feature = feature
        self._accept = accept
        self._reject = reject
        self._resolved = False

    def grant(self) -> None:
        if not self._resolved:
            self._accept()
            self._resolved = True
            self.resolved.emit(True)

    def deny(self) -> None:
        if not self._resolved:
            self._reject()
            self._resolved = True
            self.resolved.emit(False)


class BrowserPage(QWebEnginePage):
    """A secure-by-default page with permission and popup forwarding."""

    permission_prompt = Signal(object)
    blocked_navigation = Signal(str, str)
    console_message = Signal(str, int, str)

    def __init__(
        self,
        profile: QWebEngineProfile,
        security: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(profile, parent)
        self.security = security
        if hasattr(self, "permissionRequested"):
            self.permissionRequested.connect(self._modern_permission_requested)
        if hasattr(self, "featurePermissionRequested"):
            self.featurePermissionRequested.connect(self._legacy_permission_requested)
        if hasattr(self, "certificateError"):
            self.certificateError.connect(self._certificate_error)

    def acceptNavigationRequest(self, url: QUrl, navigation_type: Any, is_main_frame: bool) -> bool:  # noqa: N802
        if not is_main_frame or self.security is None:
            return super().acceptNavigationRequest(url, navigation_type, is_main_frame)
        try:
            verdict = self.security.check_url(url.toString())
            allowed = verdict if isinstance(verdict, bool) else bool(getattr(verdict, "allowed", True))
            reason = "" if isinstance(verdict, bool) else str(getattr(verdict, "reason", ""))
        except Exception:
            LOGGER.exception("Security URL check failed")
            allowed, reason = True, ""
        if not allowed:
            self.blocked_navigation.emit(url.toString(), reason)
            return False
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)

    def javaScriptConsoleMessage(self, level: Any, message: str, line: int, source: str) -> None:  # noqa: N802
        level_name = getattr(level, "name", str(level))
        self.console_message.emit(message, line, source)
        LOGGER.debug("Web console [%s] %s:%s: %s", level_name, source, line, message)

    def _modern_permission_requested(self, permission: Any) -> None:
        try:
            origin = permission.origin().toString()
            feature = getattr(permission.permissionType(), "name", str(permission.permissionType()))
            request = PermissionRequest(origin, feature, permission.grant, permission.deny, self)
            self.permission_prompt.emit(request)
        except Exception:
            LOGGER.exception("Could not process WebEngine permission")
            if hasattr(permission, "deny"):
                permission.deny()

    def _legacy_permission_requested(self, origin: QUrl, feature: Any) -> None:
        policy = QWebEnginePage.PermissionPolicy
        request = PermissionRequest(
            origin.toString(),
            getattr(feature, "name", str(feature)),
            lambda: self.setFeaturePermission(origin, feature, policy.PermissionGrantedByUser),
            lambda: self.setFeaturePermission(origin, feature, policy.PermissionDeniedByUser),
            self,
        )
        self.permission_prompt.emit(request)

    @staticmethod
    def _certificate_error(error: Any) -> None:
        LOGGER.warning("Certificate error for %s: %s", error.url().toString(), error.description())
        if hasattr(error, "rejectCertificate"):
            error.rejectCertificate()


class BrowserView(QWebEngineView):
    """Web view that delegates new windows back to the tab controller."""

    new_view_created = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.new_window_factory: Callable[[Any], BrowserView] | None = None

    def createWindow(self, window_type: Any) -> BrowserView:  # noqa: N802 - Qt API
        if self.new_window_factory is not None:
            view = self.new_window_factory(window_type)
            self.new_view_created.emit(view)
            return view
        view = BrowserView(self.window())
        self.new_view_created.emit(view)
        return view


class BrowserDownload(QObject):
    """Observable download item with speed and progress helpers."""

    changed = Signal()
    finished = Signal()

    def __init__(self, request: QWebEngineDownloadRequest, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.request = request
        self.id = f"qt-{id(request):x}"
        self.started_at = time.monotonic()
        self._last_at = self.started_at
        self._last_bytes = 0
        self._finished_emitted = False
        self.speed = 0.0
        for signal_name in (
            "receivedBytesChanged",
            "totalBytesChanged",
            "stateChanged",
            "isPausedChanged",
            "downloadFileNameChanged",
        ):
            signal = getattr(request, signal_name, None)
            if signal is not None:
                signal.connect(self._on_change)

    @property
    def filename(self) -> str:
        return self.request.downloadFileName()

    @property
    def directory(self) -> str:
        return self.request.downloadDirectory()

    @property
    def path(self) -> Path:
        return Path(self.directory) / self.filename

    @property
    def received(self) -> int:
        return int(self.request.receivedBytes())

    @property
    def total(self) -> int:
        return int(self.request.totalBytes())

    @property
    def progress(self) -> float:
        return self.received / self.total if self.total > 0 else -1.0

    @property
    def state(self) -> str:
        return getattr(self.request.state(), "name", str(self.request.state()))

    @property
    def paused(self) -> bool:
        return bool(self.request.isPaused())

    def accept(self, directory: str | Path | None = None, filename: str | None = None) -> None:
        if directory is not None:
            Path(directory).mkdir(parents=True, exist_ok=True)
            self.request.setDownloadDirectory(str(directory))
        if filename:
            self.request.setDownloadFileName(filename)
        self.request.accept()

    def pause(self) -> None:
        self.request.pause()

    def resume(self) -> None:
        self.request.resume()

    def cancel(self) -> None:
        self.request.cancel()

    def open_file(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))

    def open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def _on_change(self, *_: Any) -> None:
        now = time.monotonic()
        current = self.received
        elapsed = now - self._last_at
        if elapsed >= 0.25:
            instant = max(0.0, (current - self._last_bytes) / elapsed)
            self.speed = instant if self.speed <= 0 else self.speed * 0.65 + instant * 0.35
            self._last_at, self._last_bytes = now, current
        self.changed.emit()
        terminal = (
            "Completed" in self.state
            or "Cancelled" in self.state
            or "Interrupted" in self.state
        )
        if terminal and not self._finished_emitted:
            self._finished_emitted = True
            self.finished.emit()


class BrowserEngine(QObject):
    """Creates persistent Chromium profiles, pages, views and downloads."""

    download_created = Signal(object)
    permission_requested = Signal(object)
    notification_requested = Signal(object)
    request_blocked = Signal(str, str)

    def __init__(
        self,
        resource_root: str | Path,
        *,
        security: Any | None = None,
        adblocker: Any | None = None,
        search_template: str = "https://www.google.com/search?q={query}",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.resource_root = Path(resource_root)
        self.security = security
        self.adblocker = adblocker
        self.search_template = search_template
        self._profiles: dict[str, QWebEngineProfile] = {}
        self._profile_objects: dict[str, tuple[InternalSchemeHandler, RequestInterceptor]] = {}
        self._downloads: dict[str, BrowserDownload] = {}

    @property
    def downloads(self) -> tuple[BrowserDownload, ...]:
        return tuple(self._downloads.values())

    def set_search_template(self, template: str) -> None:
        if "{query}" not in template:
            raise ValueError("Search template must contain {query}")
        self.search_template = template

    def search_url(self, query: str) -> str:
        return self.search_template.format(query=quote_plus(query.strip()))

    def create_profile(
        self, profile_id: str, paths: ProfilePaths, *, off_the_record: bool = False
    ) -> QWebEngineProfile:
        if profile_id in self._profiles:
            return self._profiles[profile_id]
        paths.ensure()
        profile = (
            QWebEngineProfile(self) if off_the_record else QWebEngineProfile(f"auralis-{profile_id}", self)
        )
        if not off_the_record:
            profile.setPersistentStoragePath(str(paths.storage))
            profile.setCachePath(str(paths.cache))
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setDownloadPath(str(paths.downloads))
        profile.setHttpCacheMaximumSize(512 * 1024 * 1024)
        settings = profile.settings()
        for attribute, enabled in (
            (QWebEngineSettings.WebAttribute.JavascriptEnabled, True),
            (QWebEngineSettings.WebAttribute.LocalStorageEnabled, True),
            (QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True),
            (QWebEngineSettings.WebAttribute.PdfViewerEnabled, True),
            (QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True),
            (QWebEngineSettings.WebAttribute.ErrorPageEnabled, True),
            (QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True),
        ):
            settings.setAttribute(attribute, enabled)
        handler = InternalSchemeHandler(self.resource_root, self.search_url, profile)
        profile.installUrlSchemeHandler(INTERNAL_SCHEME, handler)
        interceptor = RequestInterceptor(self.adblocker, parent=profile)
        interceptor.blocked.connect(self.request_blocked)
        profile.setUrlRequestInterceptor(interceptor)
        profile.downloadRequested.connect(self._download_requested)
        presenter = getattr(profile, "setNotificationPresenter", None)
        if presenter is not None:
            presenter(self._notification_presenter)
        self._profiles[profile_id] = profile
        self._profile_objects[profile_id] = (handler, interceptor)
        return profile

    def profile(self, profile_id: str) -> QWebEngineProfile | None:
        return self._profiles.get(profile_id)

    def create_view(
        self,
        profile_id: str,
        *,
        parent: QObject | None = None,
        page: BrowserPage | None = None,
    ) -> BrowserView:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"Unknown WebEngine profile: {profile_id}")
        view = BrowserView(parent)
        browser_page = page or BrowserPage(profile, self.security, view)
        browser_page.permission_prompt.connect(self.permission_requested)
        view.setPage(browser_page)
        return view

    def clear_cache(self, profile_id: str) -> None:
        profile = self._profiles.get(profile_id)
        if profile is not None:
            profile.clearHttpCache()

    def clear_site_data(self, profile_id: str, *, cookies: bool = True) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        if cookies:
            profile.cookieStore().deleteAllCookies()
        profile.clearHttpCache()

    def set_cookie_policy(self, profile_id: str, *, allow_third_party: bool) -> None:
        """Apply a profile-wide cookie filter without rebuilding Chromium state."""

        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        profile.cookieStore().setCookieFilter(
            lambda request: allow_third_party or not bool(request.thirdParty)
        )

    def set_do_not_track(self, profile_id: str, enabled: bool) -> None:
        objects = self._profile_objects.get(profile_id)
        if objects is not None:
            objects[1].do_not_track = bool(enabled)

    def set_page_preloading(self, profile_id: str, enabled: bool) -> None:
        profile = self._profiles.get(profile_id)
        if profile is not None:
            profile.settings().setAttribute(
                QWebEngineSettings.WebAttribute.DnsPrefetchEnabled, bool(enabled)
            )

    def shutdown(self) -> None:
        for profile in self._profiles.values():
            profile.cookieStore().deleteSessionCookies()

    def _download_requested(self, request: QWebEngineDownloadRequest) -> None:
        download = BrowserDownload(request, self)
        self._downloads[download.id] = download
        download.finished.connect(lambda item=download: LOGGER.info("Download finished: %s", item.path))
        self.download_created.emit(download)

    def _notification_presenter(self, notification: Any) -> None:
        self.notification_requested.emit(notification)
        try:
            notification.show()
        except Exception:
            LOGGER.exception("Could not show web notification")
