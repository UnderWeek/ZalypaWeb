"""Backend-neutral asynchronous browser data synchronization primitives."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


class SyncCollection(StrEnum):
    BOOKMARKS = "bookmarks"
    HISTORY = "history"
    SETTINGS = "settings"
    TABS = "tabs"


@dataclass(frozen=True, slots=True)
class SyncAccount:
    account_id: str
    display_name: str
    credentials: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """A backend-neutral record; tombstones use ``deleted=True``."""

    record_id: str
    collection: SyncCollection
    payload: Mapping[str, Any]
    modified_at: datetime
    deleted: bool = False
    revision: str | None = None
    device_id: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("Sync record id cannot be empty")
        object.__setattr__(self, "collection", SyncCollection(self.collection))
        object.__setattr__(self, "modified_at", _parse_datetime(self.modified_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "collection": self.collection.value,
            "payload": dict(self.payload),
            "modified_at": self.modified_at.isoformat(),
            "deleted": self.deleted,
            "revision": self.revision,
            "device_id": self.device_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SyncRecord:
        return cls(
            record_id=str(payload["id"]),
            collection=SyncCollection(payload["collection"]),
            payload=dict(payload.get("payload", {})),
            modified_at=_parse_datetime(payload["modified_at"]),
            deleted=bool(payload.get("deleted", False)),
            revision=str(payload["revision"]) if payload.get("revision") is not None else None,
            device_id=str(payload["device_id"]) if payload.get("device_id") else None,
        )


@dataclass(frozen=True, slots=True)
class SyncBatch:
    records: tuple[SyncRecord, ...]
    cursor: str | None


@dataclass(frozen=True, slots=True)
class SyncPushResult:
    accepted: int
    cursor: str | None


@dataclass(frozen=True, slots=True)
class SyncResult:
    collection: SyncCollection
    pulled: int
    pushed: int
    cursor: str | None
    completed_at: datetime


class SyncBackend(ABC):
    """Interface implemented by cloud, self-hosted, or local sync backends."""

    @abstractmethod
    async def authenticate(self, account: SyncAccount) -> None:
        """Validate credentials and prepare the remote account."""

    @abstractmethod
    async def pull(self, collection: SyncCollection, cursor: str | None) -> SyncBatch:
        """Fetch remote changes strictly newer than ``cursor``."""

    @abstractmethod
    async def push(self, collection: SyncCollection, records: Sequence[SyncRecord]) -> SyncPushResult:
        """Upload local records and return the backend's newest cursor."""

    async def close(self) -> None:
        """Release backend resources.  Stateless backends need not override it."""

        return None


@runtime_checkable
class SyncDataAdapter(Protocol):
    """Bridge implemented by a repository for one sync collection."""

    async def collect_changes(self, since: datetime | None) -> Sequence[SyncRecord]: ...

    async def apply_remote(self, records: Sequence[SyncRecord]) -> None: ...


class SyncManager:
    """Coordinates repository adapters and an arbitrary :class:`SyncBackend`."""

    def __init__(
        self,
        backend: SyncBackend,
        state_path: str | Path | None = None,
        *,
        device_id: str | None = None,
    ) -> None:
        self.backend = backend
        self.state_path = Path(state_path) if state_path is not None else None
        self.device_id = device_id or uuid4().hex
        self.account: SyncAccount | None = None
        self._adapters: dict[SyncCollection, SyncDataAdapter] = {}
        self._locks: dict[SyncCollection, asyncio.Lock] = {}
        self._state_lock = threading.RLock()
        self._state: dict[str, dict[str, str | None]] = {}
        self._account_states: dict[str, dict[str, dict[str, str | None]]] = {}
        self._load_state()

    @property
    def connected(self) -> bool:
        return self.account is not None

    def register_adapter(self, collection: SyncCollection | str, adapter: SyncDataAdapter) -> None:
        normalized = SyncCollection(collection)
        if not isinstance(adapter, SyncDataAdapter):
            raise TypeError("Adapter must implement collect_changes() and apply_remote()")
        self._adapters[normalized] = adapter
        self._locks.setdefault(normalized, asyncio.Lock())

    def unregister_adapter(self, collection: SyncCollection | str) -> None:
        normalized = SyncCollection(collection)
        self._adapters.pop(normalized, None)
        self._locks.pop(normalized, None)

    async def connect(self, account: SyncAccount) -> None:
        await self.backend.authenticate(account)
        with self._state_lock:
            self.account = account
            self._state = self._account_states.setdefault(account.account_id, {})

    async def disconnect(self) -> None:
        await self.backend.close()
        self.account = None

    async def sync(self, collections: Sequence[SyncCollection | str] | None = None) -> tuple[SyncResult, ...]:
        if self.account is None:
            raise RuntimeError("Sync account is not connected")
        selected = (
            tuple(SyncCollection(item) for item in collections)
            if collections is not None
            else tuple(self._adapters)
        )
        missing = [item.value for item in selected if item not in self._adapters]
        if missing:
            raise KeyError(f"No sync adapter registered for: {', '.join(missing)}")
        results = await asyncio.gather(*(self.sync_collection(item) for item in selected))
        return tuple(results)

    async def sync_collection(self, collection: SyncCollection | str) -> SyncResult:
        if self.account is None:
            raise RuntimeError("Sync account is not connected")
        normalized = SyncCollection(collection)
        try:
            adapter = self._adapters[normalized]
        except KeyError as exc:
            raise KeyError(f"No sync adapter registered for {normalized.value}") from exc

        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            started_at = _utc_now()
            state = self._state.get(normalized.value, {})
            cursor = state.get("cursor")
            last_synced_raw = state.get("last_synced_at")
            last_synced = _parse_datetime(last_synced_raw) if last_synced_raw else None

            remote = await self.backend.pull(normalized, cursor)
            if remote.records:
                await adapter.apply_remote(remote.records)

            local = tuple(await adapter.collect_changes(last_synced))
            for record in local:
                if record.collection is not normalized:
                    raise ValueError(
                        f"Adapter for {normalized.value} emitted {record.collection.value} record"
                    )
            push_result = await self.backend.push(normalized, local)
            # Advance only to a cursor we have actually pulled through.  A
            # backend may receive a concurrent remote write between pull and
            # push; adopting the push response cursor could otherwise skip it.
            newest_cursor = remote.cursor if remote.cursor is not None else cursor
            with self._state_lock:
                self._state[normalized.value] = {
                    "cursor": newest_cursor,
                    # Use the start boundary so changes created during this sync
                    # are guaranteed to be collected by the next run.
                    "last_synced_at": started_at.isoformat(),
                }
                self._save_state()
            return SyncResult(
                collection=normalized,
                pulled=len(remote.records),
                pushed=push_result.accepted,
                cursor=newest_cursor,
                completed_at=_utc_now(),
            )

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("accounts"), dict):
                raise ValueError("Unsupported sync state version")
            stored_device = payload.get("device_id")
            if stored_device:
                self.device_id = str(stored_device)
            self._account_states = {
                str(account_id): {
                    SyncCollection(key).value: {
                        "cursor": value.get("cursor"),
                        "last_synced_at": value.get("last_synced_at"),
                    }
                    for key, value in collections.items()
                }
                for account_id, collections in payload["accounts"].items()
                if isinstance(collections, dict)
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.exception("Could not read sync state from %s", self.state_path)
            self._account_states = {}
            self._state = {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="sync-", suffix=".tmp", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "device_id": self.device_id,
                        "accounts": self._account_states,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


class InMemorySyncBackend(SyncBackend):
    """Deterministic backend useful for tests and offline product demos."""

    def __init__(self) -> None:
        self.account: SyncAccount | None = None
        self._revision = 0
        self._records: dict[SyncCollection, dict[str, tuple[int, SyncRecord]]] = {
            collection: {} for collection in SyncCollection
        }
        self._lock = asyncio.Lock()

    async def authenticate(self, account: SyncAccount) -> None:
        if not account.account_id.strip():
            raise ValueError("Account id cannot be empty")
        self.account = account

    async def pull(self, collection: SyncCollection, cursor: str | None) -> SyncBatch:
        if self.account is None:
            raise RuntimeError("Backend is not authenticated")
        after = int(cursor or 0)
        async with self._lock:
            items = [
                (revision, record)
                for revision, record in self._records[collection].values()
                if revision > after
            ]
            items.sort(key=lambda item: item[0])
            newest = str(self._revision)
            return SyncBatch(tuple(record for _, record in items), newest)

    async def push(self, collection: SyncCollection, records: Sequence[SyncRecord]) -> SyncPushResult:
        if self.account is None:
            raise RuntimeError("Backend is not authenticated")
        accepted = 0
        async with self._lock:
            for incoming in records:
                if incoming.collection is not collection:
                    raise ValueError("Record collection does not match push collection")
                existing = self._records[collection].get(incoming.record_id)
                if existing is not None and existing[1].modified_at > incoming.modified_at:
                    continue
                self._revision += 1
                stored = SyncRecord(
                    record_id=incoming.record_id,
                    collection=collection,
                    payload=dict(incoming.payload),
                    modified_at=incoming.modified_at,
                    deleted=incoming.deleted,
                    revision=str(self._revision),
                    device_id=incoming.device_id,
                )
                self._records[collection][incoming.record_id] = (self._revision, stored)
                accepted += 1
            return SyncPushResult(accepted=accepted, cursor=str(self._revision))

    async def close(self) -> None:
        self.account = None
