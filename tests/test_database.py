from __future__ import annotations

from pathlib import Path

from browser.database import (
    BookmarksRepository,
    DownloadsRepository,
    DownloadStatus,
    HistoryRepository,
    SettingsRepository,
    SQLiteDatabase,
)


def test_profile_database_workflow(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "profile.sqlite3")
    history = HistoryRepository(database)
    bookmarks = BookmarksRepository(database)
    downloads = DownloadsRepository(database)
    settings = SettingsRepository(database)

    history.add_visit("https://example.com/a", "Example A", transition="typed")
    history.add_visit("https://example.com/a", "Example A again")
    history.add_visit("https://python.org", "Python")
    history.record_search("material browser", search_engine="google")
    assert history.count() == 3
    assert history.suggestions("exam")[0].visit_count == 2
    assert history.list_searches("material")[0].use_count == 1

    folder = bookmarks.create_folder("Работа")
    bookmark = bookmarks.add(
        "https://example.com/a",
        "Example",
        folder_id=folder.id,
        tags=("docs", "work"),
    )
    assert bookmarks.is_bookmarked(bookmark.url)
    assert bookmarks.search("docs")[0].id == bookmark.id

    record = downloads.create(
        "https://example.com/file.zip",
        tmp_path / "file.zip",
        total_bytes=100,
        status=DownloadStatus.IN_PROGRESS,
    )
    record = downloads.update_progress(record.id, 50, bytes_per_second=25)
    assert record.progress == 0.5
    assert downloads.complete(record.id).status is DownloadStatus.COMPLETED

    settings.set_many(
        {"appearance.theme": "dark", "general.restore_session": True},
        namespace="browser",
    )
    assert settings.get("appearance.theme", namespace="browser") == "dark"
    assert database.integrity_check() == ("ok",)
    database.close()


def test_bookmark_round_trip(tmp_path: Path) -> None:
    source_db = SQLiteDatabase(tmp_path / "source.sqlite3")
    source = BookmarksRepository(source_db)
    folder = source.create_folder("Коллекция")
    source.add("https://qt.io", "Qt", folder_id=folder.id)
    exported = tmp_path / "bookmarks.json"
    source.export_json(exported)

    target_db = SQLiteDatabase(tmp_path / "target.sqlite3")
    target = BookmarksRepository(target_db)
    result = target.import_json(exported)
    assert result.folders_imported == 1
    assert result.bookmarks_imported == 1
    assert target.search("Qt")[0].url == "https://qt.io"
    source_db.close()
    target_db.close()
