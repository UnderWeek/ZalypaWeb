"""SQLite connection management and schema migrations.

The browser keeps all profile-local structured data in one SQLite database.  A
connection is deliberately opened per operation: this avoids sharing a
``sqlite3.Connection`` between Qt and worker threads while WAL mode still makes
concurrent readers inexpensive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import sqlite3
import threading
from types import TracebackType
from typing import Any, Final, Self
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

DEFAULT_DATABASE_NAME: Final = "browser.sqlite3"
LATEST_SCHEMA_VERSION: Final = 2


class DatabaseError(RuntimeError):
    """Base error raised by the browser persistence layer."""


class MigrationError(DatabaseError):
    """Raised when a schema migration cannot be applied safely."""


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(UTC)


def datetime_to_storage(value: datetime) -> str:
    """Serialize a datetime as a stable, lexicographically sortable UTC value."""

    if value.tzinfo is None:
        # A naive timestamp is interpreted as UTC, never as the machine's local
        # timezone.  This keeps profile databases portable across timezones.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def datetime_from_storage(value: str) -> datetime:
    """Parse a timestamp written by :func:`datetime_to_storage`."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f"Invalid timestamp in database: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_database_path(
    path: str | os.PathLike[str], *, default_name: str = DEFAULT_DATABASE_NAME
) -> Path | str:
    """Resolve either a database filename or an existing profile directory.

    ``":memory:"`` is preserved.  Existing directories are resolved to
    ``<directory>/browser.sqlite3``.  A non-existing path is treated as the
    desired database filename so that an accidental typo cannot silently create
    an unexpected directory.
    """

    if os.fspath(path) == ":memory:":
        return ":memory:"
    candidate = Path(path).expanduser()
    if candidate.exists() and candidate.is_dir():
        return candidate / default_name
    return candidate


@dataclass(frozen=True, slots=True)
class Migration:
    """A single ordered schema migration."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Migration versions must start at 1")


def _migration_001_initial_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE history_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL CHECK (length(url) > 0),
            title TEXT NOT NULL DEFAULT '',
            visited_at TEXT NOT NULL,
            transition TEXT NOT NULL DEFAULT 'link',
            is_hidden INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1))
        )
        """,
        """
        CREATE TABLE search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL COLLATE NOCASE CHECK (length(query) > 0),
            search_engine TEXT NOT NULL DEFAULT 'default' COLLATE NOCASE,
            first_searched_at TEXT NOT NULL,
            last_searched_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 1 CHECK (use_count > 0),
            UNIQUE (query, search_engine)
        )
        """,
        """
        CREATE TABLE bookmark_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES bookmark_folders(id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK (length(name) > 0),
            position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER REFERENCES bookmark_folders(id) ON DELETE SET NULL,
            url TEXT NOT NULL CHECK (length(url) > 0),
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL CHECK (length(url) > 0),
            referrer TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL,
            suggested_filename TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            total_bytes INTEGER NOT NULL DEFAULT -1 CHECK (total_bytes >= -1),
            received_bytes INTEGER NOT NULL DEFAULT 0 CHECK (received_bytes >= 0),
            bytes_per_second REAL NOT NULL DEFAULT 0 CHECK (bytes_per_second >= 0),
            status TEXT NOT NULL DEFAULT 'queued',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            error_message TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE settings (
            namespace TEXT NOT NULL COLLATE NOCASE,
            key TEXT NOT NULL COLLATE NOCASE,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace, key)
        ) WITHOUT ROWID
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_002_indexes(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE INDEX idx_history_visited_at ON history_entries(visited_at DESC, id DESC)",
        "CREATE INDEX idx_history_url ON history_entries(url COLLATE NOCASE)",
        "CREATE INDEX idx_search_history_recent ON search_history(last_searched_at DESC, id DESC)",
        "CREATE INDEX idx_folders_parent_position ON bookmark_folders(parent_id, position, id)",
        "CREATE INDEX idx_bookmarks_folder_position ON bookmarks(folder_id, position, id)",
        "CREATE INDEX idx_bookmarks_url ON bookmarks(url COLLATE NOCASE)",
        "CREATE INDEX idx_downloads_status_updated ON downloads(status, updated_at DESC, id DESC)",
        "CREATE INDEX idx_downloads_started ON downloads(started_at DESC, id DESC)",
    )
    for statement in statements:
        connection.execute(statement)


DEFAULT_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "initial schema", _migration_001_initial_schema),
    Migration(2, "query indexes", _migration_002_indexes),
)


class SQLiteDatabase:
    """Thread-safe factory for short-lived SQLite connections.

    Args:
        path: A database filename, ``":memory:"``, or an existing profile
            directory.  Existing directories receive ``browser.sqlite3``.
        timeout: Busy timeout, in seconds.
        migrations: Ordered migrations.  Defaults to the browser schema.
        initialize: Apply migrations immediately when true.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = 10.0,
        migrations: Sequence[Migration] = DEFAULT_MIGRATIONS,
        initialize: bool = True,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        resolved = resolve_database_path(path)
        self.path = resolved
        self.timeout = float(timeout)
        self._migrations = tuple(migrations)
        self._write_lock = threading.RLock()
        self._migration_lock = threading.Lock()
        self._closed = False
        self._keeper_connection: sqlite3.Connection | None = None

        versions = [migration.version for migration in self._migrations]
        if versions != sorted(set(versions)):
            raise ValueError("Migrations must have unique ascending versions")

        if resolved == ":memory:":
            # A shared-cache URI plus one keeper connection retains the in-memory
            # database while operation-scoped connections come and go.
            self._connect_target = f"file:browser-{uuid4().hex}?mode=memory&cache=shared"
            self._uses_uri = True
            self._keeper_connection = self._open_connection()
        else:
            database_path = Path(resolved)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connect_target = str(database_path)
            self._uses_uri = False

        if initialize:
            self.migrate()

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError("Database has been closed")

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._connect_target,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
            uri=self._uses_uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured read connection and always close it."""

        self._ensure_open()
        connection = self._open_connection()
        try:
            yield connection
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a write transaction, rolling it back on any exception."""

        self._ensure_open()
        with self._write_lock:
            connection = self._open_connection()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    LOGGER.exception("Failed to roll back SQLite transaction")
                raise
            finally:
                connection.close()

    def migrate(self) -> int:
        """Apply pending migrations atomically and return the schema version."""

        self._ensure_open()
        with self._migration_lock, self._write_lock:
            connection = self._open_connection()
            try:
                # WAL is persistent and cannot be changed inside a transaction.
                if self.path != ":memory:":
                    connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                rows = connection.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                ).fetchall()
                applied = {int(row["version"]): str(row["name"]) for row in rows}
                known_versions = {migration.version for migration in self._migrations}
                unknown = set(applied).difference(known_versions)
                if unknown:
                    raise MigrationError(
                        "Database schema is newer or incompatible; unknown migrations: "
                        + ", ".join(map(str, sorted(unknown)))
                    )

                for migration in self._migrations:
                    if migration.version in applied:
                        continue
                    LOGGER.info(
                        "Applying database migration %d (%s)",
                        migration.version,
                        migration.name,
                    )
                    migration.apply(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            datetime_to_storage(utc_now()),
                        ),
                    )
                connection.execute(
                    f"PRAGMA user_version = {max(known_versions, default=0)}"
                )
                connection.commit()
                return max(known_versions, default=0)
            except Exception as exc:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    LOGGER.exception("Failed to roll back database migration")
                if isinstance(exc, MigrationError):
                    raise
                raise MigrationError(f"Could not migrate database: {exc}") from exc
            finally:
                connection.close()

    @property
    def schema_version(self) -> int:
        """Return the greatest applied migration version."""

        with self.connection() as connection:
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row["version"]) if row is not None else 0

    def integrity_check(self) -> tuple[str, ...]:
        """Return the output of SQLite's integrity checker (``('ok',)`` normally)."""

        with self.connection() as connection:
            return tuple(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )

    def checkpoint(self, *, truncate: bool = False) -> tuple[int, int, int]:
        """Checkpoint WAL data and return SQLite's status tuple."""

        mode = "TRUNCATE" if truncate else "PASSIVE"
        with self._write_lock, self.connection() as connection:
            row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2])

    def vacuum(self) -> None:
        """Compact the database.  This can be expensive and blocks writers."""

        with self._write_lock, self.connection() as connection:
            connection.execute("VACUUM")

    def close(self) -> None:
        """Release the in-memory keeper connection, if present."""

        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            if self._keeper_connection is not None:
                self._keeper_connection.close()
                self._keeper_connection = None

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class Repository:
    """Shared ownership and context-manager behaviour for repositories."""

    def __init__(self, database: SQLiteDatabase | str | os.PathLike[str]) -> None:
        self._owns_database = not isinstance(database, SQLiteDatabase)
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError(f"{type(self).__name__} has been closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_database:
            self.database.close()

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def validate_pagination(limit: int, offset: int) -> None:
    """Validate common repository pagination arguments."""

    if isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def escape_like(value: str) -> str:
    """Escape a value for a ``LIKE ... ESCAPE '\\'`` expression."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


RowMapping = sqlite3.Row | dict[str, Any]

