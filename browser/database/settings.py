"""JSON-backed profile settings and per-site permission decisions."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar, cast
from urllib.parse import urlsplit

from .connection import (
    Repository,
    SQLiteDatabase,
    datetime_from_storage,
    datetime_to_storage,
    utc_now,
)

JSONScalar = None | bool | int | float | str
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
T = TypeVar("T")
_MISSING = object()


class PermissionDecision(StrEnum):
    ASK = "ask"
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class SettingRecord:
    namespace: str
    key: str
    value: JSONValue
    updated_at: datetime


def _validate_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be empty")
    if "\x00" in cleaned:
        raise ValueError(f"{label} cannot contain a NUL character")
    return cleaned


def _serialize(value: JSONValue) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Setting value must be valid JSON data") from exc


def _deserialize(raw: str) -> JSONValue:
    try:
        return cast(JSONValue, json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("Corrupt JSON setting value") from exc


def _record_from_row(row: sqlite3.Row) -> SettingRecord:
    return SettingRecord(
        namespace=str(row["namespace"]),
        key=str(row["key"]),
        value=_deserialize(str(row["value_json"])),
        updated_at=datetime_from_storage(str(row["updated_at"])),
    )


def normalise_origin(origin: str) -> str:
    """Return a canonical HTTP(S) security origin."""

    value = origin.strip()
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("origin must be a valid HTTP(S) origin")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin contains an invalid port") from exc
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{port_suffix}"


class SettingsRepository(Repository):
    """Thread-safe namespaced settings store with atomic bulk updates."""

    SITE_PERMISSIONS_NAMESPACE = "site_permissions"

    def __init__(self, database: SQLiteDatabase | str | os.PathLike[str]) -> None:
        super().__init__(database)

    def get(
        self,
        key: str,
        default: T | None = None,
        *,
        namespace: str = "general",
    ) -> JSONValue | T | None:
        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        return default if row is None else _deserialize(str(row["value_json"]))

    def require(self, key: str, *, namespace: str = "general") -> JSONValue:
        """Return a value or raise ``KeyError`` when it has not been configured."""

        marker = object()
        value = self.get(key, marker, namespace=namespace)
        if value is marker:
            raise KeyError(f"Missing setting {namespace}.{key}")
        return cast(JSONValue, value)

    def get_typed(
        self,
        key: str,
        expected_type: type[T],
        default: T | object = _MISSING,
        *,
        namespace: str = "general",
    ) -> T:
        """Read a setting and verify its runtime type.

        ``bool`` is not accepted as ``int`` even though it is an ``int`` subclass.
        """

        marker = object()
        value = self.get(key, marker, namespace=namespace)
        if value is marker:
            if default is _MISSING:
                raise KeyError(f"Missing setting {namespace}.{key}")
            return cast(T, default)
        valid = isinstance(value, expected_type)
        if expected_type is int and isinstance(value, bool):
            valid = False
        if not valid:
            raise TypeError(
                f"Setting {namespace}.{key} is {type(value).__name__}, expected {expected_type.__name__}"
            )
        return cast(T, value)

    def get_record(self, key: str, *, namespace: str = "general") -> SettingRecord | None:
        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def set(
        self,
        key: str,
        value: JSONValue,
        *,
        namespace: str = "general",
    ) -> SettingRecord:
        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        encoded = _serialize(value)
        timestamp = datetime_to_storage(utc_now())
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (namespace, key, encoded, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        assert row is not None
        return _record_from_row(row)

    def set_many(
        self,
        values: Mapping[str, JSONValue],
        *,
        namespace: str = "general",
    ) -> tuple[SettingRecord, ...]:
        """Upsert multiple settings in one transaction and key-sorted order."""

        self._ensure_open()
        namespace = _validate_identifier(namespace, "namespace")
        prepared = [(_validate_identifier(key, "key"), _serialize(value)) for key, value in values.items()]
        prepared.sort(key=lambda item: (item[0].casefold(), item[0]))
        if not prepared:
            return ()
        timestamp = datetime_to_storage(utc_now())
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO settings(namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                ((namespace, key, raw, timestamp) for key, raw in prepared),
            )
            placeholders = ", ".join("?" for _ in prepared)
            rows = connection.execute(
                f"""
                SELECT * FROM settings
                WHERE namespace = ? AND key IN ({placeholders})
                ORDER BY key COLLATE NOCASE ASC, key ASC
                """,
                (namespace, *(key for key, _ in prepared)),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    update = set_many

    def mutate(
        self,
        key: str,
        mutator: Callable[[JSONValue | None], JSONValue],
        *,
        namespace: str = "general",
    ) -> SettingRecord:
        """Read-modify-write a setting under one write transaction."""

        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
            current = None if row is None else _deserialize(str(row["value_json"]))
            value = mutator(current)
            encoded = _serialize(value)
            timestamp = datetime_to_storage(utc_now())
            connection.execute(
                """
                INSERT INTO settings(namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (namespace, key, encoded, timestamp),
            )
            result = connection.execute(
                "SELECT * FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        assert result is not None
        return _record_from_row(result)

    def delete(self, key: str, *, namespace: str = "general") -> bool:
        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
        return cursor.rowcount > 0

    def contains(self, key: str, *, namespace: str = "general") -> bool:
        self._ensure_open()
        key = _validate_identifier(key, "key")
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM settings WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        return row is not None

    def get_all(self, *, namespace: str = "general") -> dict[str, JSONValue]:
        self._ensure_open()
        namespace = _validate_identifier(namespace, "namespace")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT key, value_json FROM settings WHERE namespace = ?
                ORDER BY key COLLATE NOCASE ASC, key ASC
                """,
                (namespace,),
            ).fetchall()
        return {str(row["key"]): _deserialize(str(row["value_json"])) for row in rows}

    all = get_all

    def records(self, *, namespace: str | None = None) -> tuple[SettingRecord, ...]:
        self._ensure_open()
        parameters: tuple[str, ...] = ()
        where = ""
        if namespace is not None:
            namespace = _validate_identifier(namespace, "namespace")
            where = " WHERE namespace = ?"
            parameters = (namespace,)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM settings{where}
                ORDER BY namespace COLLATE NOCASE ASC, namespace ASC,
                         key COLLATE NOCASE ASC, key ASC
                """,
                parameters,
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def clear(self, *, namespace: str | None = None) -> int:
        self._ensure_open()
        query = "DELETE FROM settings"
        parameters: tuple[str, ...] = ()
        if namespace is not None:
            namespace = _validate_identifier(namespace, "namespace")
            query += " WHERE namespace = ?"
            parameters = (namespace,)
        with self.database.transaction() as connection:
            cursor = connection.execute(query, parameters)
        return max(cursor.rowcount, 0)

    def set_site_permission(
        self,
        origin: str,
        permission: str,
        decision: PermissionDecision | str,
    ) -> SettingRecord:
        canonical = normalise_origin(origin)
        permission = _validate_identifier(permission, "permission").lower()
        try:
            actual_decision = (
                decision if isinstance(decision, PermissionDecision) else PermissionDecision(decision.lower())
            )
        except ValueError as exc:
            raise ValueError("decision must be 'ask', 'allow', or 'block'") from exc

        def change(current: JSONValue | None) -> JSONValue:
            permissions = dict(current) if isinstance(current, dict) else {}
            permissions[permission] = actual_decision.value
            return cast(JSONValue, permissions)

        return self.mutate(
            canonical,
            change,
            namespace=self.SITE_PERMISSIONS_NAMESPACE,
        )

    def get_site_permission(
        self,
        origin: str,
        permission: str,
        default: PermissionDecision | str = PermissionDecision.ASK,
    ) -> PermissionDecision:
        canonical = normalise_origin(origin)
        permission = _validate_identifier(permission, "permission").lower()
        actual_default = _coerce_permission(default)
        value = self.get(canonical, {}, namespace=self.SITE_PERMISSIONS_NAMESPACE)
        if not isinstance(value, dict):
            return actual_default
        raw = value.get(permission)
        try:
            return PermissionDecision(raw) if isinstance(raw, str) else actual_default
        except ValueError:
            return actual_default

    def delete_site_permission(self, origin: str, permission: str | None = None) -> bool:
        canonical = normalise_origin(origin)
        if permission is None:
            return self.delete(canonical, namespace=self.SITE_PERMISSIONS_NAMESPACE)
        permission = _validate_identifier(permission, "permission").lower()
        removed = False

        def change(current: JSONValue | None) -> JSONValue:
            nonlocal removed
            permissions = dict(current) if isinstance(current, dict) else {}
            removed = permissions.pop(permission, None) is not None
            return cast(JSONValue, permissions)

        record = self.mutate(canonical, change, namespace=self.SITE_PERMISSIONS_NAMESPACE)
        if record.value == {}:
            self.delete(canonical, namespace=self.SITE_PERMISSIONS_NAMESPACE)
        return removed

    def list_site_permissions(self) -> dict[str, dict[str, PermissionDecision]]:
        raw = self.get_all(namespace=self.SITE_PERMISSIONS_NAMESPACE)
        result: dict[str, dict[str, PermissionDecision]] = {}
        for origin, value in raw.items():
            if not isinstance(value, dict):
                continue
            decisions: dict[str, PermissionDecision] = {}
            for permission, decision in value.items():
                if not isinstance(decision, str):
                    continue
                try:
                    decisions[permission] = PermissionDecision(decision)
                except ValueError:
                    continue
            if decisions:
                result[origin] = decisions
        return result


def _coerce_permission(value: PermissionDecision | str) -> PermissionDecision:
    try:
        return value if isinstance(value, PermissionDecision) else PermissionDecision(value)
    except ValueError as exc:
        raise ValueError("permission decision must be 'ask', 'allow', or 'block'") from exc


SettingsDatabase = SettingsRepository
SettingRepository = SettingsRepository


__all__ = [
    "JSONScalar",
    "JSONValue",
    "PermissionDecision",
    "SettingRecord",
    "SettingRepository",
    "SettingsDatabase",
    "SettingsRepository",
    "normalise_origin",
]
