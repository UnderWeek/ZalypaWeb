"""Persistent download metadata and state transitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .connection import (
    Repository,
    SQLiteDatabase,
    datetime_from_storage,
    datetime_to_storage,
    escape_like,
    utc_now,
    validate_pagination,
)


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DownloadStatus.COMPLETED,
            DownloadStatus.CANCELLED,
            DownloadStatus.FAILED,
        }


ACTIVE_DOWNLOAD_STATUSES = frozenset(
    {
        DownloadStatus.QUEUED,
        DownloadStatus.IN_PROGRESS,
        DownloadStatus.PAUSED,
        DownloadStatus.INTERRUPTED,
    }
)


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    id: int
    guid: str
    url: str
    referrer: str
    file_path: Path
    suggested_filename: str
    mime_type: str
    total_bytes: int
    received_bytes: int
    bytes_per_second: float
    status: DownloadStatus
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    error_message: str

    @property
    def progress(self) -> float | None:
        """A fraction in ``[0, 1]``, or ``None`` when total size is unknown."""

        if self.total_bytes <= 0:
            return None
        return min(max(self.received_bytes / self.total_bytes, 0.0), 1.0)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_DOWNLOAD_STATUSES


def _coerce_status(value: DownloadStatus | str) -> DownloadStatus:
    try:
        return value if isinstance(value, DownloadStatus) else DownloadStatus(value)
    except ValueError as exc:
        choices = ", ".join(status.value for status in DownloadStatus)
        raise ValueError(f"Unknown download status {value!r}; expected {choices}") from exc


def _download_from_row(row: sqlite3.Row) -> DownloadRecord:
    try:
        status = DownloadStatus(str(row["status"]))
    except ValueError as exc:
        raise ValueError(f"Corrupt download status: {row['status']!r}") from exc
    return DownloadRecord(
        id=int(row["id"]),
        guid=str(row["guid"]),
        url=str(row["url"]),
        referrer=str(row["referrer"]),
        file_path=Path(str(row["file_path"])),
        suggested_filename=str(row["suggested_filename"]),
        mime_type=str(row["mime_type"]),
        total_bytes=int(row["total_bytes"]),
        received_bytes=int(row["received_bytes"]),
        bytes_per_second=float(row["bytes_per_second"]),
        status=status,
        started_at=datetime_from_storage(str(row["started_at"])),
        updated_at=datetime_from_storage(str(row["updated_at"])),
        finished_at=(
            datetime_from_storage(str(row["finished_at"]))
            if row["finished_at"] is not None
            else None
        ),
        error_message=str(row["error_message"]),
    )


class DownloadsRepository(Repository):
    """Thread-safe persisted view of Qt WebEngine download items."""

    def __init__(
        self, database: SQLiteDatabase | str | os.PathLike[str]
    ) -> None:
        super().__init__(database)

    def create(
        self,
        url: str,
        file_path: str | os.PathLike[str],
        *,
        guid: str | None = None,
        referrer: str = "",
        suggested_filename: str = "",
        mime_type: str = "application/octet-stream",
        total_bytes: int = -1,
        started_at: datetime | None = None,
        status: DownloadStatus | str = DownloadStatus.QUEUED,
    ) -> DownloadRecord:
        self._ensure_open()
        url = url.strip()
        guid = (guid or uuid4().hex).strip()
        mime_type = mime_type.strip() or "application/octet-stream"
        if not url:
            raise ValueError("url cannot be empty")
        if not guid:
            raise ValueError("guid cannot be empty")
        if total_bytes < -1:
            raise ValueError("total_bytes must be -1 (unknown) or non-negative")
        actual_status = _coerce_status(status)
        timestamp = datetime_to_storage(started_at or utc_now())
        finished_at = timestamp if actual_status.is_terminal else None
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO downloads(
                    guid, url, referrer, file_path, suggested_filename, mime_type,
                    total_bytes, received_bytes, bytes_per_second, status,
                    started_at, updated_at, finished_at, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, '')
                """,
                (
                    guid,
                    url,
                    referrer.strip(),
                    os.fspath(file_path),
                    suggested_filename.strip(),
                    mime_type,
                    total_bytes,
                    actual_status.value,
                    timestamp,
                    timestamp,
                    finished_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return _download_from_row(row)

    create_download = create
    add = create

    def get(self, download_id: int) -> DownloadRecord | None:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
        return _download_from_row(row) if row is not None else None

    get_download = get

    def get_by_guid(self, guid: str) -> DownloadRecord | None:
        self._ensure_open()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE guid = ?", (guid.strip(),)
            ).fetchone()
        return _download_from_row(row) if row is not None else None

    def list(
        self,
        *,
        statuses: DownloadStatus | str | Iterable[DownloadStatus | str] | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[DownloadRecord]:
        self._ensure_open()
        validate_pagination(limit, offset)
        clauses: list[str] = []
        parameters: list[object] = []
        if statuses is not None:
            if isinstance(statuses, (DownloadStatus, str)):
                status_values = (_coerce_status(statuses).value,)
            else:
                status_values = tuple(_coerce_status(value).value for value in statuses)
            if not status_values:
                return []
            placeholders = ", ".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(status_values)
        if query.strip():
            pattern = f"%{escape_like(query.strip())}%"
            clauses.append(
                "(url LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR file_path LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR suggested_filename LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parameters.extend((pattern, pattern, pattern))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM downloads{where}
                ORDER BY started_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_download_from_row(row) for row in rows]

    list_downloads = list

    def active(self) -> list[DownloadRecord]:
        return self.list(statuses=ACTIVE_DOWNLOAD_STATUSES, limit=10_000)

    active_downloads = active

    def update_progress(
        self,
        download_id: int,
        received_bytes: int,
        *,
        total_bytes: int | None = None,
        bytes_per_second: float | None = None,
        updated_at: datetime | None = None,
    ) -> DownloadRecord:
        self._ensure_open()
        if received_bytes < 0:
            raise ValueError("received_bytes must be non-negative")
        if total_bytes is not None and total_bytes < -1:
            raise ValueError("total_bytes must be -1 or non-negative")
        if bytes_per_second is not None and bytes_per_second < 0:
            raise ValueError("bytes_per_second must be non-negative")
        timestamp = datetime_to_storage(updated_at or utc_now())
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Download {download_id} does not exist")
            actual_total = (
                int(current["total_bytes"]) if total_bytes is None else total_bytes
            )
            if actual_total >= 0 and received_bytes > actual_total:
                # Servers occasionally revise Content-Length downward.  Preserve
                # truthful received bytes by growing the stored total.
                actual_total = received_bytes
            actual_speed = (
                float(current["bytes_per_second"])
                if bytes_per_second is None
                else float(bytes_per_second)
            )
            current_status = _coerce_status(str(current["status"]))
            next_status = (
                DownloadStatus.IN_PROGRESS
                if current_status == DownloadStatus.QUEUED
                else current_status
            )
            connection.execute(
                """
                UPDATE downloads SET
                    received_bytes = ?, total_bytes = ?, bytes_per_second = ?,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    received_bytes,
                    actual_total,
                    actual_speed,
                    next_status.value,
                    timestamp,
                    download_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
        assert row is not None
        return _download_from_row(row)

    def set_status(
        self,
        download_id: int,
        status: DownloadStatus | str,
        *,
        error_message: str = "",
        changed_at: datetime | None = None,
    ) -> DownloadRecord:
        self._ensure_open()
        actual_status = _coerce_status(status)
        timestamp = datetime_to_storage(changed_at or utc_now())
        finished_at = timestamp if actual_status.is_terminal else None
        speed = 0.0 if actual_status != DownloadStatus.IN_PROGRESS else None
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT 1 FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Download {download_id} does not exist")
            connection.execute(
                """
                UPDATE downloads SET
                    status = ?, updated_at = ?, finished_at = ?,
                    error_message = ?,
                    bytes_per_second = COALESCE(?, bytes_per_second)
                WHERE id = ?
                """,
                (
                    actual_status.value,
                    timestamp,
                    finished_at,
                    error_message.strip(),
                    speed,
                    download_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
        assert row is not None
        return _download_from_row(row)

    def pause(self, download_id: int) -> DownloadRecord:
        return self.set_status(download_id, DownloadStatus.PAUSED)

    def resume(self, download_id: int) -> DownloadRecord:
        return self.set_status(download_id, DownloadStatus.IN_PROGRESS)

    def cancel(self, download_id: int) -> DownloadRecord:
        return self.set_status(download_id, DownloadStatus.CANCELLED)

    def complete(
        self, download_id: int, *, finished_at: datetime | None = None
    ) -> DownloadRecord:
        return self.set_status(
            download_id, DownloadStatus.COMPLETED, changed_at=finished_at
        )

    def fail(
        self,
        download_id: int,
        error_message: str,
        *,
        failed_at: datetime | None = None,
    ) -> DownloadRecord:
        return self.set_status(
            download_id,
            DownloadStatus.FAILED,
            error_message=error_message,
            changed_at=failed_at,
        )

    def mark_active_interrupted(self) -> int:
        """Mark downloads left running after a crash as resumable interruptions."""

        self._ensure_open()
        timestamp = datetime_to_storage(utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE downloads SET status = ?, bytes_per_second = 0, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    DownloadStatus.INTERRUPTED.value,
                    timestamp,
                    DownloadStatus.QUEUED.value,
                    DownloadStatus.IN_PROGRESS.value,
                ),
            )
        return max(cursor.rowcount, 0)

    def delete(self, download_id: int, *, allow_active: bool = False) -> bool:
        self._ensure_open()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
            if row is None:
                return False
            if not allow_active and _coerce_status(str(row["status"])) in ACTIVE_DOWNLOAD_STATUSES:
                raise ValueError("Cannot remove an active download record")
            cursor = connection.execute(
                "DELETE FROM downloads WHERE id = ?", (download_id,)
            )
        return cursor.rowcount > 0

    delete_download = delete

    def clear_finished(self) -> int:
        self._ensure_open()
        values = tuple(status.value for status in DownloadStatus if status.is_terminal)
        placeholders = ", ".join("?" for _ in values)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"DELETE FROM downloads WHERE status IN ({placeholders})", values
            )
        return max(cursor.rowcount, 0)

    def count(self, *, statuses: Iterable[DownloadStatus | str] | None = None) -> int:
        self._ensure_open()
        parameters: tuple[str, ...] = ()
        where = ""
        if statuses is not None:
            parameters = tuple(_coerce_status(value).value for value in statuses)
            if not parameters:
                return 0
            placeholders = ", ".join("?" for _ in parameters)
            where = f" WHERE status IN ({placeholders})"
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM downloads{where}", parameters
            ).fetchone()
        assert row is not None
        return int(row["count"])


DownloadRepository = DownloadsRepository
DownloadsDatabase = DownloadsRepository
DownloadDatabase = DownloadsRepository


__all__ = [
    "ACTIVE_DOWNLOAD_STATUSES",
    "DownloadDatabase",
    "DownloadRecord",
    "DownloadRepository",
    "DownloadStatus",
    "DownloadsDatabase",
    "DownloadsRepository",
]

