"""Profile-local browsing and search history persistence."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .connection import (
    Repository,
    SQLiteDatabase,
    datetime_from_storage,
    datetime_to_storage,
    escape_like,
    utc_now,
    validate_pagination,
)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: int
    url: str
    title: str
    visited_at: datetime
    transition: str
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class HistorySuggestion:
    url: str
    title: str
    last_visited_at: datetime
    visit_count: int


@dataclass(frozen=True, slots=True)
class SearchHistoryEntry:
    id: int
    query: str
    search_engine: str
    first_searched_at: datetime
    last_searched_at: datetime
    use_count: int


def _history_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=int(row["id"]),
        url=str(row["url"]),
        title=str(row["title"]),
        visited_at=datetime_from_storage(str(row["visited_at"])),
        transition=str(row["transition"]),
        is_hidden=bool(row["is_hidden"]),
    )


def _search_from_row(row: sqlite3.Row) -> SearchHistoryEntry:
    return SearchHistoryEntry(
        id=int(row["id"]),
        query=str(row["query"]),
        search_engine=str(row["search_engine"]),
        first_searched_at=datetime_from_storage(str(row["first_searched_at"])),
        last_searched_at=datetime_from_storage(str(row["last_searched_at"])),
        use_count=int(row["use_count"]),
    )


class HistoryRepository(Repository):
    """Thread-safe access to browsing and omnibox-search history."""

    def __init__(self, database: SQLiteDatabase | str | os.PathLike[str]) -> None:
        super().__init__(database)

    def add_visit(
        self,
        url: str,
        title: str = "",
        *,
        visited_at: datetime | None = None,
        transition: str = "link",
        hidden: bool = False,
    ) -> HistoryEntry:
        """Append one visit; repeated URLs intentionally remain separate visits."""

        self._ensure_open()
        url = url.strip()
        transition = transition.strip()
        if not url:
            raise ValueError("url cannot be empty")
        if not transition:
            raise ValueError("transition cannot be empty")
        timestamp = datetime_to_storage(visited_at or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO history_entries(url, title, visited_at, transition, is_hidden)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url, title.strip(), timestamp, transition, int(hidden)),
            )
            row = connection.execute(
                "SELECT * FROM history_entries WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return _history_from_row(row)

    record_visit = add_visit

    def get(self, entry_id: int) -> HistoryEntry | None:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM history_entries WHERE id = ?", (entry_id,)).fetchone()
        return _history_from_row(row) if row is not None else None

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        start: datetime | None = None,
        end: datetime | None = None,
        include_hidden: bool = False,
    ) -> list[HistoryEntry]:
        """List visits newest-first with deterministic ID tie-breaking."""

        self._ensure_open()
        validate_pagination(limit, offset)
        clauses: list[str] = []
        parameters: list[object] = []
        if not include_hidden:
            clauses.append("is_hidden = 0")
        if start is not None:
            clauses.append("visited_at >= ?")
            parameters.append(datetime_to_storage(start))
        if end is not None:
            clauses.append("visited_at <= ?")
            parameters.append(datetime_to_storage(end))
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be later than end")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM history_entries{where}
                ORDER BY visited_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_history_from_row(row) for row in rows]

    get_recent = list

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        include_hidden: bool = False,
    ) -> list[HistoryEntry]:
        """Search URL and title using a literal, case-insensitive substring."""

        self._ensure_open()
        validate_pagination(limit, offset)
        query = query.strip()
        if not query:
            return self.list(limit=limit, offset=offset, include_hidden=include_hidden)
        pattern = f"%{escape_like(query)}%"
        hidden_clause = "" if include_hidden else " AND is_hidden = 0"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM history_entries
                WHERE (url LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR title LIKE ? ESCAPE '\\' COLLATE NOCASE)
                      {hidden_clause}
                ORDER BY visited_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (pattern, pattern, limit, offset),
            ).fetchall()
        return [_history_from_row(row) for row in rows]

    def suggestions(self, text: str, *, limit: int = 10) -> list[HistorySuggestion]:
        """Return deduplicated omnibox suggestions ordered by relevance/recency."""

        self._ensure_open()
        validate_pagination(limit, 0)
        text = text.strip()
        literal = escape_like(text)
        prefix = f"{literal}%"
        contains = f"%{literal}%"
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    h.url,
                    COALESCE((
                        SELECT latest.title
                        FROM history_entries AS latest
                        WHERE latest.url = h.url AND latest.is_hidden = 0
                        ORDER BY latest.visited_at DESC, latest.id DESC
                        LIMIT 1
                    ), '') AS title,
                    MAX(h.visited_at) AS last_visited_at,
                    COUNT(*) AS visit_count,
                    CASE
                        WHEN h.url LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 0
                        WHEN MAX(h.title) LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1
                        ELSE 2
                    END AS relevance
                FROM history_entries AS h
                WHERE h.is_hidden = 0
                  AND (h.url LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR h.title LIKE ? ESCAPE '\\' COLLATE NOCASE)
                GROUP BY h.url COLLATE NOCASE
                ORDER BY relevance ASC, visit_count DESC,
                         last_visited_at DESC, h.url COLLATE NOCASE ASC
                LIMIT ?
                """,
                (prefix, prefix, contains, contains, limit),
            ).fetchall()
        return [
            HistorySuggestion(
                url=str(row["url"]),
                title=str(row["title"]),
                last_visited_at=datetime_from_storage(str(row["last_visited_at"])),
                visit_count=int(row["visit_count"]),
            )
            for row in rows
        ]

    suggest = suggestions

    def count(self, *, include_hidden: bool = False) -> int:
        self._ensure_open()
        where = "" if include_hidden else " WHERE is_hidden = 0"
        with self.database.connection() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM history_entries{where}").fetchone()
        assert row is not None
        return int(row["count"])

    def delete(self, entry_id: int) -> bool:
        self._ensure_open()
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM history_entries WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0

    delete_entry = delete

    def delete_url(self, url: str) -> int:
        self._ensure_open()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM history_entries WHERE url = ? COLLATE NOCASE",
                (url.strip(),),
            )
        return max(cursor.rowcount, 0)

    def delete_between(self, start: datetime, end: datetime) -> int:
        self._ensure_open()
        if start > end:
            raise ValueError("start must not be later than end")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM history_entries WHERE visited_at BETWEEN ? AND ?",
                (datetime_to_storage(start), datetime_to_storage(end)),
            )
        return max(cursor.rowcount, 0)

    def clear(self, *, before: datetime | None = None) -> int:
        """Delete all visits, or visits at/before ``before``; return row count."""

        self._ensure_open()
        query = "DELETE FROM history_entries"
        parameters: tuple[object, ...] = ()
        if before is not None:
            query += " WHERE visited_at <= ?"
            parameters = (datetime_to_storage(before),)
        with self.database.transaction() as connection:
            cursor = connection.execute(query, parameters)
        return max(cursor.rowcount, 0)

    clear_history = clear

    def record_search(
        self,
        query: str,
        *,
        search_engine: str = "default",
        searched_at: datetime | None = None,
    ) -> SearchHistoryEntry:
        self._ensure_open()
        query = " ".join(query.split())
        search_engine = search_engine.strip()
        if not query:
            raise ValueError("query cannot be empty")
        if not search_engine:
            raise ValueError("search_engine cannot be empty")
        timestamp = datetime_to_storage(searched_at or utc_now())
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO search_history(
                    query, search_engine, first_searched_at, last_searched_at, use_count
                ) VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(query, search_engine) DO UPDATE SET
                    last_searched_at = excluded.last_searched_at,
                    use_count = search_history.use_count + 1
                """,
                (query, search_engine, timestamp, timestamp),
            )
            row = connection.execute(
                """
                SELECT * FROM search_history
                WHERE query = ? COLLATE NOCASE
                  AND search_engine = ? COLLATE NOCASE
                """,
                (query, search_engine),
            ).fetchone()
        assert row is not None
        return _search_from_row(row)

    def list_searches(
        self,
        query: str = "",
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchHistoryEntry]:
        self._ensure_open()
        validate_pagination(limit, offset)
        parameters: list[object] = []
        where = ""
        if query.strip():
            where = " WHERE query LIKE ? ESCAPE '\\' COLLATE NOCASE"
            parameters.append(f"%{escape_like(query.strip())}%")
        parameters.extend((limit, offset))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM search_history{where}
                ORDER BY last_searched_at DESC, use_count DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_search_from_row(row) for row in rows]

    search_queries = list_searches

    def delete_search(self, entry_id: int) -> bool:
        self._ensure_open()
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM search_history WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0

    def clear_search_history(self) -> int:
        self._ensure_open()
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM search_history")
        return max(cursor.rowcount, 0)


# Compatibility name for callers that model repositories as per-feature databases.
HistoryDatabase = HistoryRepository


__all__ = [
    "HistoryDatabase",
    "HistoryEntry",
    "HistoryRepository",
    "HistorySuggestion",
    "SearchHistoryEntry",
]
