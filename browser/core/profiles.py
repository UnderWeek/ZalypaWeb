"""Browser profile registry and isolated profile filesystem layouts.

The module deliberately has no Qt dependency.  A UI profile switcher and the
Qt WebEngine profile adapter can both consume the same :class:`BrowserProfile`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Iterable
from uuid import uuid4

logger = logging.getLogger(__name__)

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_REGISTRY_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def default_data_root() -> Path:
    """Return the platform data directory for Auralis Browser."""

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "AuralisBrowser"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "auralis-browser"


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    """Canonical locations owned by one browser profile."""

    root: Path
    database: Path
    webengine_storage: Path
    cache: Path
    downloads: Path
    extensions: Path
    permissions: Path

    @classmethod
    def from_root(cls, root: Path) -> "ProfilePaths":
        return cls(
            root=root,
            database=root / "browser.sqlite3",
            webengine_storage=root / "webengine",
            cache=root / "cache",
            downloads=root / "downloads",
            extensions=root / "extensions",
            permissions=root / "permissions.json",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.webengine_storage,
            self.cache,
            self.downloads,
            self.extensions,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """Immutable metadata and filesystem layout for a browser user."""

    profile_id: str
    name: str
    paths: ProfilePaths
    created_at: datetime
    last_used_at: datetime
    avatar_path: Path | None = None

    @property
    def id(self) -> str:
        """Compatibility shorthand for callers that prefer ``profile.id``."""

        return self.profile_id

    def to_registry_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "directory": self.paths.root.name,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "last_used_at": self.last_used_at.astimezone(UTC).isoformat(),
            "avatar_path": str(self.avatar_path) if self.avatar_path else None,
        }


class ProfileManager:
    """Owns the profile registry and creates isolated on-disk layouts.

    Registry updates are atomic within a process.  Individual profile databases
    provide their own cross-thread SQLite locking.
    """

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else default_data_root()
        self.profiles_root = self.data_root / "profiles"
        self.registry_path = self.data_root / "profiles.json"
        self._lock = threading.RLock()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        self._profiles: dict[str, BrowserProfile] = {}
        self._active_profile_id: str | None = None
        self._load_registry()

    @property
    def active_profile_id(self) -> str | None:
        with self._lock:
            return self._active_profile_id

    @property
    def active_profile(self) -> BrowserProfile | None:
        with self._lock:
            if self._active_profile_id is None:
                return None
            return self._profiles.get(self._active_profile_id)

    def ensure_default_profile(self, name: str = "Основной") -> BrowserProfile:
        """Return the active profile, creating the first profile if necessary."""

        with self._lock:
            profile = self.active_profile
            if profile is not None:
                profile.paths.ensure()
                return profile
            if self._profiles:
                first = min(self._profiles.values(), key=lambda item: item.created_at)
                return self.activate(first.profile_id)
            return self.create_profile(name)

    def list_profiles(self) -> tuple[BrowserProfile, ...]:
        with self._lock:
            return tuple(sorted(self._profiles.values(), key=lambda item: (item.created_at, item.name)))

    def get_profile(self, profile_id: str) -> BrowserProfile:
        with self._lock:
            try:
                return self._profiles[profile_id]
            except KeyError as exc:
                raise KeyError(f"Unknown profile: {profile_id}") from exc

    def create_profile(
        self,
        name: str,
        *,
        avatar_path: str | Path | None = None,
        profile_id: str | None = None,
        activate: bool = True,
    ) -> BrowserProfile:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Profile name cannot be empty")
        if len(clean_name) > 80:
            raise ValueError("Profile name cannot exceed 80 characters")
        new_id = profile_id or f"profile-{uuid4().hex[:12]}"
        if not _PROFILE_ID_RE.fullmatch(new_id):
            raise ValueError("Profile id must contain 3-64 lowercase letters, digits, '_' or '-'")

        with self._lock:
            if new_id in self._profiles:
                raise ValueError(f"Profile already exists: {new_id}")
            paths = ProfilePaths.from_root(self.profiles_root / new_id)
            paths.ensure()
            now = _utc_now()
            profile = BrowserProfile(
                profile_id=new_id,
                name=clean_name,
                paths=paths,
                created_at=now,
                last_used_at=now,
                avatar_path=Path(avatar_path).expanduser() if avatar_path else None,
            )
            self._profiles[new_id] = profile
            if activate or self._active_profile_id is None:
                self._active_profile_id = new_id
            try:
                self._save_registry()
            except Exception:
                self._profiles.pop(new_id, None)
                if self._active_profile_id == new_id:
                    self._active_profile_id = next(iter(self._profiles), None)
                shutil.rmtree(paths.root, ignore_errors=True)
                raise
            logger.info("Created browser profile %s", new_id)
            return profile

    def activate(self, profile_id: str) -> BrowserProfile:
        with self._lock:
            profile = self.get_profile(profile_id)
            profile.paths.ensure()
            updated = replace(profile, last_used_at=_utc_now())
            self._profiles[profile_id] = updated
            self._active_profile_id = profile_id
            self._save_registry()
            return updated

    def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        avatar_path: str | Path | None = None,
        clear_avatar: bool = False,
    ) -> BrowserProfile:
        with self._lock:
            current = self.get_profile(profile_id)
            clean_name = current.name if name is None else name.strip()
            if not clean_name or len(clean_name) > 80:
                raise ValueError("Profile name must contain 1-80 characters")
            avatar = current.avatar_path
            if clear_avatar:
                avatar = None
            elif avatar_path is not None:
                avatar = Path(avatar_path).expanduser()
            updated = replace(current, name=clean_name, avatar_path=avatar)
            self._profiles[profile_id] = updated
            self._save_registry()
            return updated

    def delete_profile(self, profile_id: str, *, delete_files: bool = True) -> None:
        with self._lock:
            profile = self.get_profile(profile_id)
            previous_active = self._active_profile_id
            del self._profiles[profile_id]
            if previous_active == profile_id:
                self._active_profile_id = next(iter(self._profiles), None)
            self._save_registry()

            if delete_files and profile.paths.root.exists():
                root = profile.paths.root.resolve()
                profiles_root = self.profiles_root.resolve()
                if root.parent != profiles_root:
                    raise RuntimeError(f"Refusing to remove profile outside {profiles_root}")
                shutil.rmtree(root)
            logger.info("Deleted browser profile %s", profile_id)

    def _load_registry(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if payload.get("version") != _REGISTRY_VERSION:
                raise ValueError("Unsupported profile registry version")
            profiles: dict[str, BrowserProfile] = {}
            for item in payload.get("profiles", []):
                profile_id = str(item["id"])
                directory = str(item.get("directory", profile_id))
                if not _PROFILE_ID_RE.fullmatch(profile_id) or directory != profile_id:
                    raise ValueError(f"Invalid profile registry entry: {profile_id!r}")
                paths = ProfilePaths.from_root(self.profiles_root / directory)
                profiles[profile_id] = BrowserProfile(
                    profile_id=profile_id,
                    name=str(item["name"]),
                    paths=paths,
                    created_at=_as_utc(item["created_at"]),
                    last_used_at=_as_utc(item["last_used_at"]),
                    avatar_path=Path(item["avatar_path"]) if item.get("avatar_path") else None,
                )
            active_id = payload.get("active_profile_id")
            self._profiles = profiles
            self._active_profile_id = active_id if active_id in profiles else next(iter(profiles), None)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            logger.error("Could not load profile registry %s: %s", self.registry_path, exc)
            backup = self.registry_path.with_suffix(f".corrupt-{int(_utc_now().timestamp())}.json")
            try:
                shutil.copy2(self.registry_path, backup)
            except OSError:
                logger.exception("Could not back up corrupt profile registry")
            self._profiles = {}
            self._active_profile_id = None

    def _save_registry(self) -> None:
        payload = {
            "version": _REGISTRY_VERSION,
            "active_profile_id": self._active_profile_id,
            "profiles": [profile.to_registry_dict() for profile in self.list_profiles()],
        }
        self.data_root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="profiles-", suffix=".tmp", dir=self.data_root, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.registry_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


# Concise product-domain name for view models and plugin APIs.
Profile = BrowserProfile


__all__ = [
    "BrowserProfile",
    "Profile",
    "ProfileManager",
    "ProfilePaths",
    "default_data_root",
]
