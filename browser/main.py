"""Application entry point for Auralis Browser."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QProcess, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from browser.core.browser_engine import BrowserEngine, register_internal_scheme
from browser.core.browser_engine import ProfilePaths as EngineProfilePaths
from browser.core.profiles import BrowserProfile, ProfileManager
from browser.core.security import SecurityManager, SitePermissionStore
from browser.database import (
    BookmarksRepository,
    DownloadsRepository,
    HistoryRepository,
    SettingsRepository,
    SQLiteDatabase,
)
from browser.services.adblock import AdBlocker
from browser.services.extensions import ExtensionManager
from browser.ui.main_window import SEARCH_ENGINES, SETTINGS_NAMESPACE, BrowserContext, BrowserWindow
from browser.ui.material_theme import ThemeManager
from browser.ui.settings import DEFAULT_SETTINGS

LOGGER = logging.getLogger(__name__)
PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auralis Browser")
    parser.add_argument("url", nargs="?", help="Адрес, открываемый после запуска")
    parser.add_argument("--profile", help="ID профиля")
    parser.add_argument("--data-dir", type=Path, help="Переопределить каталог данных")
    parser.add_argument(
        "--incognito", action="store_true", help="Приватный профиль без постоянной БД/cookies"
    )
    parser.add_argument("--demo-data", action="store_true", help="Добавить демонстрационные записи истории")
    parser.add_argument("--smoke-test", action="store_true", help="Создать окно и автоматически завершиться")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def configure_logging(data_root: Path, level: str) -> None:
    log_dir = data_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "auralis.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level), handlers=(console, file_handler), force=True)


def select_profile(manager: ProfileManager, requested_id: str | None) -> BrowserProfile:
    if requested_id:
        profile = manager.get_profile(requested_id)
        profile.paths.ensure()
        return profile
    return manager.ensure_default_profile()


def seed_profile(
    settings: SettingsRepository,
    bookmarks: BookmarksRepository,
    history: HistoryRepository,
    *,
    demo_data: bool,
) -> None:
    values = settings.get_all(namespace=SETTINGS_NAMESPACE)
    missing_defaults = {key: value for key, value in DEFAULT_SETTINGS.items() if key not in values}
    if missing_defaults:
        settings.set_many(missing_defaults, namespace=SETTINGS_NAMESPACE)
    seed_version = int(settings.get("seed_version", 0, namespace=SETTINGS_NAMESPACE))
    if seed_version < 1:
        starter = bookmarks.create_folder("Для начала")
        for title, url in (
            ("Поиск Google", "https://www.google.com"),
            ("Wikipedia", "https://www.wikipedia.org"),
            ("GitHub", "https://github.com"),
        ):
            if not bookmarks.is_bookmarked(url):
                bookmarks.add(url, title, folder_id=starter.id)
        settings.set("seed_version", 1, namespace=SETTINGS_NAMESPACE)
    if demo_data and not history.count():
        history.add_visit("https://www.python.org", "Python", transition="typed")
        history.add_visit("https://doc.qt.io/qtforpython-6/", "Qt for Python", transition="link")
        history.record_search("Material 3 desktop browser")


def build_context(
    profile_manager: ProfileManager,
    profile: BrowserProfile,
    app: QApplication,
    *,
    incognito: bool,
    demo_data: bool,
) -> BrowserContext:
    database = SQLiteDatabase(":memory:" if incognito else profile.paths.database)
    history = HistoryRepository(database)
    bookmarks = BookmarksRepository(database)
    downloads = DownloadsRepository(database)
    settings = SettingsRepository(database)
    seed_profile(settings, bookmarks, history, demo_data=demo_data and not incognito)
    values = dict(DEFAULT_SETTINGS)
    values.update(settings.get_all(namespace=SETTINGS_NAMESPACE))

    permission_store = SitePermissionStore(None if incognito else profile.paths.permissions)
    security = SecurityManager(permission_store)
    adblocker = AdBlocker(None if incognito else profile.paths.root / "adblock.json")
    starter_filters = RESOURCE_ROOT / "starter_filters.txt"
    if starter_filters.exists():
        adblocker.load_filter_file(starter_filters, source="auralis-starter", replace_source=True)
    cached_easylist = profile.paths.root / "filters" / "easylist.txt"
    if cached_easylist.exists():
        try:
            adblocker.load_filter_file(cached_easylist, source="easylist", replace_source=True)
        except OSError:
            LOGGER.exception("Could not load cached EasyList subscription")
    adblocker.set_enabled(str(values.get("privacy.tracking_protection", "standard")) != "off")
    extensions = ExtensionManager(profile.paths.extensions)

    search_key = str(values.get("general.search_engine", "google"))
    engine = BrowserEngine(
        RESOURCE_ROOT,
        security=security,
        adblocker=adblocker,
        search_template=SEARCH_ENGINES.get(search_key, SEARCH_ENGINES["google"]),
    )
    engine_paths = EngineProfilePaths(
        root=profile.paths.root,
        storage=profile.paths.webengine_storage,
        cache=profile.paths.cache,
        downloads=Path(str(values.get("downloads.directory") or profile.paths.downloads)),
    )
    engine.create_profile(profile.id, engine_paths, off_the_record=incognito)
    engine.set_cookie_policy(
        profile.id,
        allow_third_party=not bool(values.get("privacy.third_party_cookies", True)),
    )
    engine.set_do_not_track(profile.id, bool(values.get("privacy.do_not_track", False)))
    engine.set_page_preloading(profile.id, bool(values.get("performance.preload_pages", True)))
    theme = ThemeManager(
        app,
        mode=str(values.get("appearance.theme", "system")),
        accent=str(values.get("appearance.accent", "#6750A4")),
        density=str(values.get("appearance.density", "comfortable")),
        scale=int(values.get("appearance.ui_scale", 100)),
    )
    theme.apply()
    return BrowserContext(
        profile_manager=profile_manager,
        profile=profile,
        database=database,
        history=history,
        bookmarks=bookmarks,
        downloads=downloads,
        settings=settings,
        security=security,
        adblocker=adblocker,
        extensions=extensions,
        engine=engine,
        theme=theme,
    )


def _configure_pre_application(hardware_acceleration: bool = True) -> None:
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if not hardware_acceleration and "--disable-gpu" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --disable-gpu").strip()
    register_internal_scheme()


def _configure_console_encoding() -> None:
    """Keep Russian CLI help/logs printable in legacy Windows terminals."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    profile_manager = ProfileManager(args.data_dir)
    profile = select_profile(profile_manager, args.profile)
    configure_logging(profile_manager.data_root, args.log_level)

    # Hardware acceleration must be selected before Chromium is initialized.
    hardware = True
    if not args.incognito and profile.paths.database.exists():
        try:
            preflight_database = SQLiteDatabase(profile.paths.database)
            preflight_settings = SettingsRepository(preflight_database)
            hardware = bool(
                preflight_settings.get(
                    "performance.hardware_acceleration", True, namespace=SETTINGS_NAMESPACE
                )
            )
            preflight_database.close()
        except Exception:
            LOGGER.exception("Could not read pre-application settings")
    _configure_pre_application(hardware)

    app = QApplication([sys.argv[0], *(argv if argv is not None else sys.argv[1:])])
    app.setApplicationName("Auralis Browser")
    app.setApplicationDisplayName("Auralis Browser")
    app.setOrganizationName("Auralis")
    app.setOrganizationDomain("auralis.local")
    app.setWindowIcon(QIcon(str(RESOURCE_ROOT / "app_icon.svg")))
    app.setQuitOnLastWindowClosed(True)

    context = build_context(profile_manager, profile, app, incognito=args.incognito, demo_data=args.demo_data)
    windows: list[BrowserWindow] = []

    def open_profile_window(profile_id: str, private: bool) -> None:
        command = ["-m", "browser.main", "--profile", profile_id]
        if args.data_dir:
            command.extend(("--data-dir", str(args.data_dir)))
        if private:
            command.append("--incognito")
        started = QProcess.startDetached(sys.executable, command, str(PACKAGE_ROOT.parent))[0]
        if not started:
            LOGGER.error("Could not launch profile window: %s", profile_id)

    window = BrowserWindow(context, incognito=args.incognito)
    window.profileWindowRequested.connect(open_profile_window)
    windows.append(window)
    window.show()
    if args.url:
        QTimer.singleShot(0, lambda: window.navigate(args.url))
    if args.smoke_test:
        QTimer.singleShot(2200, app.quit)

    event_loop = QEventLoop(app)
    asyncio.set_event_loop(event_loop)
    app.aboutToQuit.connect(event_loop.stop)
    exit_code = 0
    try:
        with event_loop:
            event_loop.run_forever()
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user")
    finally:
        context.engine.shutdown()
        context.database.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
