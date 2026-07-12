"""Navigation safety checks and persistent per-site permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


class URLRisk(StrEnum):
    SECURE = "secure"
    INSECURE = "insecure"
    LOCAL = "local"
    INTERNAL = "internal"
    SUSPICIOUS = "suspicious"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SecurityVerdict:
    allowed: bool
    risk: URLRisk
    normalized_url: str
    host: str | None
    reason: str | None = None

    @property
    def is_https(self) -> bool:
        return self.normalized_url.lower().startswith("https://")


class PermissionType(StrEnum):
    NOTIFICATIONS = "notifications"
    GEOLOCATION = "geolocation"
    CAMERA = "camera"
    MICROPHONE = "microphone"
    CLIPBOARD_READ = "clipboard_read"
    CLIPBOARD_WRITE = "clipboard_write"
    FULLSCREEN = "fullscreen"
    POPUPS = "popups"
    MIDI = "midi"
    DOWNLOADS = "downloads"


class PermissionDecision(StrEnum):
    ASK = "ask"
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class SitePermission:
    origin: str
    permission: PermissionType
    decision: PermissionDecision


def normalize_host(host: str) -> str:
    value = host.strip().rstrip(".").lower()
    if not value:
        raise ValueError("Host cannot be empty")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"Invalid host: {host!r}") from exc


def normalize_origin(url_or_origin: str) -> str:
    candidate = url_or_origin.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Site permissions require an HTTP(S) origin")
    host = normalize_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid origin port") from exc
    default = 80 if parsed.scheme == "http" else 443
    authority = f"{host}:{port}" if port is not None and port != default else host
    return f"{parsed.scheme.lower()}://{authority}"


class SitePermissionStore:
    """Thread-safe JSON store used directly by WebEngine permission prompts."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._permissions: dict[str, dict[str, str]] = {}
        self._load()

    def get(
        self,
        origin: str,
        permission: PermissionType | str,
        default: PermissionDecision = PermissionDecision.ASK,
    ) -> PermissionDecision:
        normalized = normalize_origin(origin)
        permission_type = PermissionType(permission)
        with self._lock:
            raw = self._permissions.get(normalized, {}).get(permission_type.value)
            return PermissionDecision(raw) if raw is not None else default

    def set(
        self,
        origin: str,
        permission: PermissionType | str,
        decision: PermissionDecision | str,
    ) -> SitePermission:
        normalized = normalize_origin(origin)
        permission_type = PermissionType(permission)
        choice = PermissionDecision(decision)
        with self._lock:
            if choice is PermissionDecision.ASK:
                site = self._permissions.get(normalized)
                if site:
                    site.pop(permission_type.value, None)
                    if not site:
                        self._permissions.pop(normalized, None)
            else:
                self._permissions.setdefault(normalized, {})[permission_type.value] = choice.value
            self._save()
        return SitePermission(normalized, permission_type, choice)

    def reset(self, origin: str, permission: PermissionType | str | None = None) -> None:
        normalized = normalize_origin(origin)
        with self._lock:
            if permission is None:
                self._permissions.pop(normalized, None)
            else:
                site = self._permissions.get(normalized)
                if site:
                    site.pop(PermissionType(permission).value, None)
                    if not site:
                        self._permissions.pop(normalized, None)
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._permissions.clear()
            self._save()

    def list_all(self) -> tuple[SitePermission, ...]:
        with self._lock:
            return tuple(
                SitePermission(origin, PermissionType(permission), PermissionDecision(decision))
                for origin, values in sorted(self._permissions.items())
                for permission, decision in sorted(values.items())
            )

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("permissions"), dict):
                raise ValueError("Unsupported permissions file")
            validated: dict[str, dict[str, str]] = {}
            for origin, values in payload["permissions"].items():
                normalized = normalize_origin(origin)
                validated[normalized] = {
                    PermissionType(key).value: PermissionDecision(value).value
                    for key, value in values.items()
                    if PermissionDecision(value) is not PermissionDecision.ASK
                }
            self._permissions = validated
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not load site permissions from %s", self.path)
            self._permissions = {}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="permissions-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "permissions": self._permissions},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class SecurityManager:
    """Evaluates navigations and exposes a profile's permission store."""

    _SAFE_SCHEMES = frozenset({"http", "https", "file", "about", "auralis"})
    _CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

    def __init__(
        self,
        permission_store: SitePermissionStore | None = None,
        *,
        blocked_hosts: Iterable[str] = (),
        allowed_schemes: Iterable[str] | None = None,
    ) -> None:
        self.permissions = permission_store or SitePermissionStore()
        self._blocked_hosts = {normalize_host(host) for host in blocked_hosts}
        self._allowed_schemes = frozenset(
            scheme.lower() for scheme in (allowed_schemes or self._SAFE_SCHEMES)
        )
        self._lock = threading.RLock()

    def add_blocked_host(self, host: str) -> None:
        with self._lock:
            self._blocked_hosts.add(normalize_host(host))

    def remove_blocked_host(self, host: str) -> None:
        with self._lock:
            self._blocked_hosts.discard(normalize_host(host))

    def is_host_blocked(self, host: str) -> bool:
        normalized = normalize_host(host)
        with self._lock:
            return any(normalized == item or normalized.endswith("." + item) for item in self._blocked_hosts)

    def check_url(self, url: str) -> SecurityVerdict:
        """Return a non-throwing security verdict for a top-level navigation."""

        candidate = url.strip()
        if not candidate:
            return SecurityVerdict(False, URLRisk.BLOCKED, "", None, "Пустой адрес")
        if len(candidate) > 8192 or self._CONTROL_CHARS.search(candidate):
            return SecurityVerdict(False, URLRisk.BLOCKED, candidate, None, "Некорректный адрес")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return SecurityVerdict(False, URLRisk.BLOCKED, candidate, None, "Некорректный адрес")
        scheme = parsed.scheme.lower()
        if not scheme:
            candidate = "https://" + candidate
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                return SecurityVerdict(False, URLRisk.BLOCKED, candidate, None, "Некорректный адрес")
            scheme = "https"
        if scheme not in self._allowed_schemes:
            return SecurityVerdict(
                False, URLRisk.BLOCKED, candidate, parsed.hostname, f"Схема {scheme!r} запрещена"
            )
        if scheme in {"about", "auralis"}:
            return SecurityVerdict(True, URLRisk.INTERNAL, candidate, None)
        if scheme == "file":
            return SecurityVerdict(True, URLRisk.LOCAL, candidate, None)

        try:
            raw_host = parsed.hostname
            if not raw_host:
                raise ValueError("missing host")
            host = normalize_host(raw_host)
            port = parsed.port
        except (ValueError, UnicodeError):
            return SecurityVerdict(False, URLRisk.BLOCKED, candidate, None, "Некорректный хост или порт")
        if self.is_host_blocked(host):
            return SecurityVerdict(False, URLRisk.BLOCKED, candidate, host, "Сайт находится в блок-листе")

        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority = f"{authority}:{port}"
        if parsed.username is not None:
            # Credentials in a navigation URL often hide the actual destination.
            return SecurityVerdict(False, URLRisk.SUSPICIOUS, candidate, host, "Учетные данные в URL")
        normalized = urlunsplit((scheme, authority, parsed.path or "/", parsed.query, parsed.fragment))
        if self._is_local_host(host):
            return SecurityVerdict(True, URLRisk.LOCAL, normalized, host)
        if host.startswith("xn--") or ".xn--" in host:
            return SecurityVerdict(True, URLRisk.SUSPICIOUS, normalized, host, "Интернационализированный домен")
        risk = URLRisk.SECURE if scheme == "https" else URLRisk.INSECURE
        reason = None if risk is URLRisk.SECURE else "Соединение не использует HTTPS"
        return SecurityVerdict(True, risk, normalized, host, reason)

    check_navigation = check_url

    @staticmethod
    def _is_local_host(host: str) -> bool:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            return True
        try:
            address = ip_address(host)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local


# A descriptive alias retained for integrations written before SecurityManager
# became the canonical application-facing name.
URLSecurityPolicy = SecurityManager
