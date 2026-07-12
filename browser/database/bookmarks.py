"""Bookmark and bookmark-folder persistence."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from .connection import (
    Repository,
    SQLiteDatabase,
    datetime_from_storage,
    datetime_to_storage,
    escape_like,
    utc_now,
    validate_pagination,
)

LOGGER = logging.getLogger(__name__)
EXPORT_FORMAT: Final = "material-browser-bookmarks-v1"
_UNSET = object()


@dataclass(frozen=True, slots=True)
class BookmarkFolder:
    id: int
    parent_id: int | None
    name: str
    position: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Bookmark:
    id: int
    folder_id: int | None
    url: str
    title: str
    description: str
    tags: tuple[str, ...]
    position: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BookmarkTreeNode:
    folder: BookmarkFolder
    folders: tuple[BookmarkTreeNode, ...]
    bookmarks: tuple[Bookmark, ...]


@dataclass(frozen=True, slots=True)
class BookmarkTree:
    folders: tuple[BookmarkTreeNode, ...]
    bookmarks: tuple[Bookmark, ...]


@dataclass(frozen=True, slots=True)
class BookmarkImportResult:
    folders_imported: int
    bookmarks_imported: int


def _decode_tags(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        LOGGER.warning("Invalid tags JSON in bookmark row", exc_info=True)
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _normalise_tags(tags: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = str(tag).strip()
        folded = cleaned.casefold()
        if cleaned and folded not in seen:
            seen.add(folded)
            result.append(cleaned)
    return tuple(result)


def _folder_from_row(row: sqlite3.Row) -> BookmarkFolder:
    return BookmarkFolder(
        id=int(row["id"]),
        parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
        name=str(row["name"]),
        position=int(row["position"]),
        created_at=datetime_from_storage(str(row["created_at"])),
        updated_at=datetime_from_storage(str(row["updated_at"])),
    )


def _bookmark_from_row(row: sqlite3.Row) -> Bookmark:
    return Bookmark(
        id=int(row["id"]),
        folder_id=int(row["folder_id"]) if row["folder_id"] is not None else None,
        url=str(row["url"]),
        title=str(row["title"]),
        description=str(row["description"]),
        tags=_decode_tags(str(row["tags_json"])),
        position=int(row["position"]),
        created_at=datetime_from_storage(str(row["created_at"])),
        updated_at=datetime_from_storage(str(row["updated_at"])),
    )


class BookmarksRepository(Repository):
    """Thread-safe repository for a hierarchy of folders and bookmarks."""

    def __init__(self, database: SQLiteDatabase | str | os.PathLike[str]) -> None:
        super().__init__(database)

    @staticmethod
    def _require_folder(connection: sqlite3.Connection, folder_id: int) -> None:
        if connection.execute("SELECT 1 FROM bookmark_folders WHERE id = ?", (folder_id,)).fetchone() is None:
            raise KeyError(f"Bookmark folder {folder_id} does not exist")

    @staticmethod
    def _next_position(
        connection: sqlite3.Connection, table: str, parent_column: str, parent_id: int | None
    ) -> int:
        if table not in {"bookmark_folders", "bookmarks"} or parent_column not in {
            "parent_id",
            "folder_id",
        }:
            raise ValueError("Invalid position target")
        row = connection.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 AS position FROM {table} WHERE {parent_column} IS ?",
            (parent_id,),
        ).fetchone()
        assert row is not None
        return int(row["position"])

    def create_folder(
        self,
        name: str,
        *,
        parent_id: int | None = None,
        position: int | None = None,
    ) -> BookmarkFolder:
        self._ensure_open()
        name = " ".join(name.split())
        if not name:
            raise ValueError("Folder name cannot be empty")
        if position is not None and position < 0:
            raise ValueError("position must be non-negative")
        timestamp = datetime_to_storage(utc_now())
        with self.database.transaction() as connection:
            if parent_id is not None:
                self._require_folder(connection, parent_id)
            actual_position = (
                position
                if position is not None
                else self._next_position(connection, "bookmark_folders", "parent_id", parent_id)
            )
            cursor = connection.execute(
                """
                INSERT INTO bookmark_folders(
                    parent_id, name, position, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (parent_id, name, actual_position, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM bookmark_folders WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return _folder_from_row(row)

    add_folder = create_folder

    def get_folder(self, folder_id: int) -> BookmarkFolder | None:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM bookmark_folders WHERE id = ?", (folder_id,)).fetchone()
        return _folder_from_row(row) if row is not None else None

    def list_folders(
        self,
        parent_id: int | None = None,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[BookmarkFolder]:
        """List direct children of ``parent_id`` (root when it is ``None``)."""

        self._ensure_open()
        validate_pagination(limit, offset)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookmark_folders
                WHERE parent_id IS ?
                ORDER BY position ASC, name COLLATE NOCASE ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (parent_id, limit, offset),
            ).fetchall()
        return [_folder_from_row(row) for row in rows]

    def all_folders(self) -> list[BookmarkFolder]:
        self._ensure_open()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookmark_folders
                ORDER BY parent_id ASC, position ASC, name COLLATE NOCASE ASC, id ASC
                """
            ).fetchall()
        return [_folder_from_row(row) for row in rows]

    def update_folder(
        self,
        folder_id: int,
        *,
        name: str | None = None,
        parent_id: int | None | object = _UNSET,
        position: int | None = None,
    ) -> BookmarkFolder:
        self._ensure_open()
        if name is not None:
            name = " ".join(name.split())
            if not name:
                raise ValueError("Folder name cannot be empty")
        if position is not None and position < 0:
            raise ValueError("position must be non-negative")
        with self.database.transaction() as connection:
            self._require_folder(connection, folder_id)
            updates: list[str] = []
            parameters: list[object] = []
            if name is not None:
                updates.append("name = ?")
                parameters.append(name)
            if parent_id is not _UNSET:
                if parent_id == folder_id:
                    raise ValueError("A folder cannot be its own parent")
                if parent_id is not None:
                    if not isinstance(parent_id, int):
                        raise TypeError("parent_id must be an integer or None")
                    self._require_folder(connection, parent_id)
                    cycle = connection.execute(
                        """
                        WITH RECURSIVE descendants(id) AS (
                            SELECT id FROM bookmark_folders WHERE parent_id = ?
                            UNION ALL
                            SELECT child.id FROM bookmark_folders AS child
                            JOIN descendants ON child.parent_id = descendants.id
                        )
                        SELECT 1 FROM descendants WHERE id = ? LIMIT 1
                        """,
                        (folder_id, parent_id),
                    ).fetchone()
                    if cycle is not None:
                        raise ValueError("Moving the folder would create a cycle")
                updates.append("parent_id = ?")
                parameters.append(parent_id)
            if position is not None:
                updates.append("position = ?")
                parameters.append(position)
            if updates:
                updates.append("updated_at = ?")
                parameters.append(datetime_to_storage(utc_now()))
                parameters.append(folder_id)
                connection.execute(
                    f"UPDATE bookmark_folders SET {', '.join(updates)} WHERE id = ?",
                    parameters,
                )
            row = connection.execute("SELECT * FROM bookmark_folders WHERE id = ?", (folder_id,)).fetchone()
        assert row is not None
        return _folder_from_row(row)

    move_folder = update_folder

    def delete_folder(self, folder_id: int, *, delete_bookmarks: bool = True) -> bool:
        """Delete a folder tree; contained bookmarks are deleted or moved to root."""

        self._ensure_open()
        with self.database.transaction() as connection:
            if delete_bookmarks:
                connection.execute(
                    """
                    WITH RECURSIVE descendants(id) AS (
                        SELECT ?
                        UNION ALL
                        SELECT child.id FROM bookmark_folders AS child
                        JOIN descendants ON child.parent_id = descendants.id
                    )
                    DELETE FROM bookmarks WHERE folder_id IN (SELECT id FROM descendants)
                    """,
                    (folder_id,),
                )
            cursor = connection.execute("DELETE FROM bookmark_folders WHERE id = ?", (folder_id,))
        return cursor.rowcount > 0

    def add(
        self,
        url: str,
        title: str = "",
        *,
        folder_id: int | None = None,
        description: str = "",
        tags: Iterable[str] = (),
        position: int | None = None,
    ) -> Bookmark:
        self._ensure_open()
        url = url.strip()
        if not url:
            raise ValueError("url cannot be empty")
        if position is not None and position < 0:
            raise ValueError("position must be non-negative")
        normalised_tags = _normalise_tags(tags)
        timestamp = datetime_to_storage(utc_now())
        with self.database.transaction() as connection:
            if folder_id is not None:
                self._require_folder(connection, folder_id)
            actual_position = (
                position
                if position is not None
                else self._next_position(connection, "bookmarks", "folder_id", folder_id)
            )
            cursor = connection.execute(
                """
                INSERT INTO bookmarks(
                    folder_id, url, title, description, tags_json, position,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    folder_id,
                    url,
                    title.strip(),
                    description.strip(),
                    json.dumps(normalised_tags, ensure_ascii=False),
                    actual_position,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM bookmarks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        assert row is not None
        return _bookmark_from_row(row)

    add_bookmark = add

    def get(self, bookmark_id: int) -> Bookmark | None:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
        return _bookmark_from_row(row) if row is not None else None

    get_bookmark = get

    def find_by_url(self, url: str) -> list[Bookmark]:
        self._ensure_open()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookmarks WHERE url = ? COLLATE NOCASE
                ORDER BY updated_at DESC, id DESC
                """,
                (url.strip(),),
            ).fetchall()
        return [_bookmark_from_row(row) for row in rows]

    def is_bookmarked(self, url: str) -> bool:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM bookmarks WHERE url = ? COLLATE NOCASE LIMIT 1",
                (url.strip(),),
            ).fetchone()
        return row is not None

    def list_bookmarks(
        self,
        folder_id: int | None | object = _UNSET,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Bookmark]:
        """List all bookmarks, or one folder when ``folder_id`` is supplied."""

        self._ensure_open()
        validate_pagination(limit, offset)
        where = ""
        parameters: list[object] = []
        if folder_id is not _UNSET:
            where = " WHERE folder_id IS ?"
            parameters.append(folder_id)
        parameters.extend((limit, offset))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM bookmarks{where}
                ORDER BY position ASC, title COLLATE NOCASE ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_bookmark_from_row(row) for row in rows]

    list = list_bookmarks

    def search(self, query: str, *, limit: int = 100, offset: int = 0) -> list[Bookmark]:
        self._ensure_open()
        validate_pagination(limit, offset)
        query = query.strip()
        if not query:
            return self.list_bookmarks(limit=limit, offset=offset)
        pattern = f"%{escape_like(query)}%"
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookmarks
                WHERE url LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR title LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR description LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR tags_json LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY
                    CASE
                        WHEN title LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 0
                        WHEN url LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1
                        ELSE 2
                    END,
                    updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    f"{escape_like(query)}%",
                    f"{escape_like(query)}%",
                    limit,
                    offset,
                ),
            ).fetchall()
        return [_bookmark_from_row(row) for row in rows]

    def update(
        self,
        bookmark_id: int,
        *,
        url: str | None = None,
        title: str | None = None,
        folder_id: int | None | object = _UNSET,
        description: str | None = None,
        tags: Iterable[str] | None = None,
        position: int | None = None,
    ) -> Bookmark:
        self._ensure_open()
        updates: list[str] = []
        parameters: list[object] = []
        if url is not None:
            url = url.strip()
            if not url:
                raise ValueError("url cannot be empty")
            updates.append("url = ?")
            parameters.append(url)
        if title is not None:
            updates.append("title = ?")
            parameters.append(title.strip())
        if description is not None:
            updates.append("description = ?")
            parameters.append(description.strip())
        if tags is not None:
            updates.append("tags_json = ?")
            parameters.append(json.dumps(_normalise_tags(tags), ensure_ascii=False))
        if position is not None:
            if position < 0:
                raise ValueError("position must be non-negative")
            updates.append("position = ?")
            parameters.append(position)
        with self.database.transaction() as connection:
            if folder_id is not _UNSET:
                if folder_id is not None:
                    if not isinstance(folder_id, int):
                        raise TypeError("folder_id must be an integer or None")
                    self._require_folder(connection, folder_id)
                updates.append("folder_id = ?")
                parameters.append(folder_id)
            if updates:
                updates.append("updated_at = ?")
                parameters.append(datetime_to_storage(utc_now()))
                parameters.append(bookmark_id)
                connection.execute(
                    f"UPDATE bookmarks SET {', '.join(updates)} WHERE id = ?",
                    parameters,
                )
            row = connection.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
            if row is None:
                raise KeyError(f"Bookmark {bookmark_id} does not exist")
        return _bookmark_from_row(row)

    update_bookmark = update

    def move(self, bookmark_id: int, folder_id: int | None, *, position: int | None = None) -> Bookmark:
        return self.update(bookmark_id, folder_id=folder_id, position=position)

    move_bookmark = move

    def delete(self, bookmark_id: int) -> bool:
        self._ensure_open()
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
        return cursor.rowcount > 0

    delete_bookmark = delete

    def clear(self) -> tuple[int, int]:
        """Delete every bookmark and folder; return ``(bookmarks, folders)``."""

        self._ensure_open()
        with self.database.transaction() as connection:
            bookmarks = connection.execute("DELETE FROM bookmarks").rowcount
            folders = connection.execute("DELETE FROM bookmark_folders").rowcount
        return max(bookmarks, 0), max(folders, 0)

    def tree(self) -> BookmarkTree:
        """Build an immutable, deterministically ordered folder tree."""

        folders = self.all_folders()
        bookmarks = self.list_bookmarks(limit=max(self.count(), 1))
        children: dict[int | None, list[BookmarkFolder]] = {}
        items: dict[int | None, list[Bookmark]] = {}
        for folder in folders:
            children.setdefault(folder.parent_id, []).append(folder)
        for bookmark in bookmarks:
            items.setdefault(bookmark.folder_id, []).append(bookmark)

        def build(folder: BookmarkFolder) -> BookmarkTreeNode:
            return BookmarkTreeNode(
                folder=folder,
                folders=tuple(build(child) for child in children.get(folder.id, ())),
                bookmarks=tuple(items.get(folder.id, ())),
            )

        return BookmarkTree(
            folders=tuple(build(folder) for folder in children.get(None, ())),
            bookmarks=tuple(items.get(None, ())),
        )

    def count(self) -> int:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM bookmarks").fetchone()
        assert row is not None
        return int(row["count"])

    def export_json(self, destination: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Return a portable payload and optionally write it as UTF-8 JSON."""

        payload: dict[str, Any] = {
            "format": EXPORT_FORMAT,
            "exported_at": datetime_to_storage(utc_now()),
            "folders": [
                {
                    "id": folder.id,
                    "parent_id": folder.parent_id,
                    "name": folder.name,
                    "position": folder.position,
                    "created_at": datetime_to_storage(folder.created_at),
                    "updated_at": datetime_to_storage(folder.updated_at),
                }
                for folder in self.all_folders()
            ],
            "bookmarks": [
                {
                    "id": bookmark.id,
                    "folder_id": bookmark.folder_id,
                    "url": bookmark.url,
                    "title": bookmark.title,
                    "description": bookmark.description,
                    "tags": list(bookmark.tags),
                    "position": bookmark.position,
                    "created_at": datetime_to_storage(bookmark.created_at),
                    "updated_at": datetime_to_storage(bookmark.updated_at),
                }
                for bookmark in self.list_bookmarks(limit=max(self.count(), 1))
            ],
        }
        if destination is not None:
            path = Path(destination).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def import_json(
        self,
        source: str | os.PathLike[str] | Mapping[str, Any],
        *,
        replace: bool = False,
    ) -> BookmarkImportResult:
        """Atomically import a payload created by :meth:`export_json`."""

        self._ensure_open()
        if isinstance(source, Mapping):
            payload: Mapping[str, Any] = source
        else:
            payload_value = json.loads(Path(source).read_text(encoding="utf-8"))
            if not isinstance(payload_value, Mapping):
                raise ValueError("Bookmark import root must be an object")
            payload = payload_value
        if payload.get("format") != EXPORT_FORMAT:
            raise ValueError("Unsupported bookmark export format")
        raw_folders = payload.get("folders", [])
        raw_bookmarks = payload.get("bookmarks", [])
        if not isinstance(raw_folders, list) or not isinstance(raw_bookmarks, list):
            raise ValueError("folders and bookmarks must be arrays")

        timestamp = datetime_to_storage(utc_now())
        folder_map: dict[int, int] = {}
        pending_parents: list[tuple[int, int | None]] = []
        with self.database.transaction() as connection:
            if replace:
                connection.execute("DELETE FROM bookmarks")
                connection.execute("DELETE FROM bookmark_folders")
            for raw in raw_folders:
                if not isinstance(raw, Mapping):
                    raise ValueError("Each folder must be an object")
                old_id = int(raw["id"])
                name = " ".join(str(raw.get("name", "")).split())
                if not name or old_id in folder_map:
                    raise ValueError("Folder IDs must be unique and names non-empty")
                position = max(int(raw.get("position", 0)), 0)
                created = str(raw.get("created_at", timestamp))
                updated = str(raw.get("updated_at", created))
                # Parse now so corrupt timestamps cannot be imported.
                datetime_from_storage(created)
                datetime_from_storage(updated)
                cursor = connection.execute(
                    """
                    INSERT INTO bookmark_folders(
                        parent_id, name, position, created_at, updated_at
                    ) VALUES (NULL, ?, ?, ?, ?)
                    """,
                    (name, position, created, updated),
                )
                new_id = int(cursor.lastrowid)
                folder_map[old_id] = new_id
                raw_parent = raw.get("parent_id")
                pending_parents.append((new_id, int(raw_parent) if raw_parent is not None else None))
            for new_id, old_parent in pending_parents:
                if old_parent is None:
                    continue
                if old_parent not in folder_map:
                    raise ValueError(f"Unknown parent folder ID {old_parent}")
                connection.execute(
                    "UPDATE bookmark_folders SET parent_id = ? WHERE id = ?",
                    (folder_map[old_parent], new_id),
                )
            # A recursive query detects self/indirect cycles after parent remapping.
            cycle = connection.execute(
                """
                WITH RECURSIVE ancestry(origin, id, parent_id) AS (
                    SELECT id, id, parent_id FROM bookmark_folders
                    UNION ALL
                    SELECT ancestry.origin, parent.id, parent.parent_id
                    FROM ancestry JOIN bookmark_folders AS parent
                      ON parent.id = ancestry.parent_id
                    WHERE ancestry.parent_id IS NOT NULL
                )
                SELECT 1 FROM ancestry WHERE origin = parent_id LIMIT 1
                """
            ).fetchone()
            if cycle is not None:
                raise ValueError("Imported folder hierarchy contains a cycle")

            imported_bookmarks = 0
            for raw in raw_bookmarks:
                if not isinstance(raw, Mapping):
                    raise ValueError("Each bookmark must be an object")
                url = str(raw.get("url", "")).strip()
                if not url:
                    raise ValueError("Imported bookmark URL cannot be empty")
                old_folder = raw.get("folder_id")
                folder_id = None
                if old_folder is not None:
                    old_folder_id = int(old_folder)
                    if old_folder_id not in folder_map:
                        raise ValueError(f"Unknown bookmark folder ID {old_folder_id}")
                    folder_id = folder_map[old_folder_id]
                tags_value = raw.get("tags", [])
                if not isinstance(tags_value, list):
                    raise ValueError("Bookmark tags must be an array")
                created = str(raw.get("created_at", timestamp))
                updated = str(raw.get("updated_at", created))
                datetime_from_storage(created)
                datetime_from_storage(updated)
                connection.execute(
                    """
                    INSERT INTO bookmarks(
                        folder_id, url, title, description, tags_json, position,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        folder_id,
                        url,
                        str(raw.get("title", "")).strip(),
                        str(raw.get("description", "")).strip(),
                        json.dumps(_normalise_tags(tags_value), ensure_ascii=False),
                        max(int(raw.get("position", 0)), 0),
                        created,
                        updated,
                    ),
                )
                imported_bookmarks += 1

        return BookmarkImportResult(len(folder_map), imported_bookmarks)


# Singular/plural compatibility names used by earlier UI layers.
BookmarkRepository = BookmarksRepository
BookmarksDatabase = BookmarksRepository
BookmarkDatabase = BookmarksRepository


__all__ = [
    "Bookmark",
    "BookmarkDatabase",
    "BookmarkFolder",
    "BookmarkImportResult",
    "BookmarkRepository",
    "BookmarkTree",
    "BookmarkTreeNode",
    "BookmarksDatabase",
    "BookmarksRepository",
]
