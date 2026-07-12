"""Validated Chrome extension manifests and an unpacked-extension registry.

Qt WebEngine does not provide Chrome's complete extension runtime.  This module
therefore owns installation, validation and state, while an
``ExtensionRuntimeAdapter`` can later implement the supported API surface and
content-script injection without changing storage or UI code.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}$")
_MESSAGE_RE = re.compile(r"^__MSG_([A-Za-z0-9_@]+)__$")


class ExtensionError(RuntimeError):
    """Base extension registry failure."""


class ExtensionManifestError(ExtensionError, ValueError):
    """Raised when a manifest is missing, unsafe, or unsupported."""


@dataclass(frozen=True, slots=True)
class ContentScript:
    matches: tuple[str, ...]
    js: tuple[str, ...] = ()
    css: tuple[str, ...] = ()
    exclude_matches: tuple[str, ...] = ()
    run_at: str = "document_idle"
    all_frames: bool = False
    match_about_blank: bool = False

    def matches_url(self, url: str) -> bool:
        return any(_match_pattern(pattern, url) for pattern in self.matches) and not any(
            _match_pattern(pattern, url) for pattern in self.exclude_matches
        )


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    manifest_version: int
    name: str
    version: str
    description: str = ""
    permissions: frozenset[str] = frozenset()
    host_permissions: frozenset[str] = frozenset()
    optional_permissions: frozenset[str] = frozenset()
    content_scripts: tuple[ContentScript, ...] = ()
    background_scripts: tuple[str, ...] = ()
    background_service_worker: str | None = None
    action: Mapping[str, Any] = field(default_factory=dict)
    icons: Mapping[int, str] = field(default_factory=dict)
    minimum_chrome_version: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Extension:
    extension_id: str
    root: Path
    manifest: ExtensionManifest
    enabled: bool
    installed_at: datetime
    managed: bool = True

    @property
    def id(self) -> str:
        return self.extension_id


class ExtensionRuntimeAdapter(Protocol):
    """Optional bridge that activates supported extension features in WebEngine."""

    def activate(self, extension: Extension) -> None: ...

    def deactivate(self, extension_id: str) -> None: ...


def _safe_relative_path(root: Path, raw: str, *, must_exist: bool = True) -> str:
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ExtensionManifestError(f"Unsafe extension path: {raw!r}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ExtensionManifestError(f"Extension path escapes its root: {raw!r}") from exc
    if must_exist and not resolved.is_file():
        raise ExtensionManifestError(f"Extension resource does not exist: {raw!r}")
    return path.as_posix()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExtensionManifestError(f"{field_name} must be an array of strings")
    return tuple(value)


def _localized(value: str, root: Path, default_locale: str | None) -> str:
    match = _MESSAGE_RE.fullmatch(value)
    if match is None or not default_locale:
        return value
    messages_file = root / "_locales" / default_locale / "messages.json"
    try:
        messages = json.loads(messages_file.read_text(encoding="utf-8"))
        entry = messages.get(match.group(1), {})
        message = entry.get("message")
        return str(message) if message else value
    except (OSError, TypeError, json.JSONDecodeError):
        logger.warning("Could not resolve localized extension message %s", value)
        return value


def _parse_content_scripts(root: Path, value: Any) -> tuple[ContentScript, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExtensionManifestError("content_scripts must be an array")
    result: list[ContentScript] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ExtensionManifestError(f"content_scripts[{index}] must be an object")
        matches = _strings(item.get("matches"), f"content_scripts[{index}].matches")
        if not matches:
            raise ExtensionManifestError(f"content_scripts[{index}] requires matches")
        js = tuple(
            _safe_relative_path(root, entry)
            for entry in _strings(item.get("js"), f"content_scripts[{index}].js")
        )
        css = tuple(
            _safe_relative_path(root, entry)
            for entry in _strings(item.get("css"), f"content_scripts[{index}].css")
        )
        if not js and not css:
            raise ExtensionManifestError(f"content_scripts[{index}] contains no scripts or styles")
        run_at = str(item.get("run_at", "document_idle"))
        if run_at not in {"document_start", "document_end", "document_idle"}:
            raise ExtensionManifestError(f"Unsupported content script run_at: {run_at}")
        result.append(
            ContentScript(
                matches=matches,
                js=js,
                css=css,
                exclude_matches=_strings(
                    item.get("exclude_matches"), f"content_scripts[{index}].exclude_matches"
                ),
                run_at=run_at,
                all_frames=bool(item.get("all_frames", False)),
                match_about_blank=bool(item.get("match_about_blank", False)),
            )
        )
    return tuple(result)


def load_manifest(extension_root: str | Path) -> ExtensionManifest:
    """Load and fully validate ``manifest.json`` from an unpacked extension."""

    root = Path(extension_root).expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ExtensionManifestError(f"Missing manifest.json in {root}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtensionManifestError(f"Could not read {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtensionManifestError("manifest.json must contain an object")

    manifest_version = payload.get("manifest_version")
    if manifest_version not in {2, 3}:
        raise ExtensionManifestError("Only Chrome manifest versions 2 and 3 are supported")
    raw_name = payload.get("name")
    version = payload.get("version")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ExtensionManifestError("Extension manifest requires a non-empty name")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ExtensionManifestError("Extension version must contain 1-4 numeric components")
    default_locale = payload.get("default_locale")
    name = _localized(raw_name.strip(), root, str(default_locale) if default_locale else None)
    description_raw = payload.get("description", "")
    if not isinstance(description_raw, str):
        raise ExtensionManifestError("Extension description must be a string")
    description = _localized(description_raw, root, str(default_locale) if default_locale else None)

    permissions = _strings(payload.get("permissions"), "permissions")
    explicit_hosts = _strings(payload.get("host_permissions"), "host_permissions")
    # Manifest V2 stores host match patterns in permissions.
    host_permissions = set(explicit_hosts)
    api_permissions: set[str] = set()
    for permission in permissions:
        if permission == "<all_urls>" or "://" in permission:
            host_permissions.add(permission)
        else:
            api_permissions.add(permission)

    content_scripts = _parse_content_scripts(root, payload.get("content_scripts"))
    background = payload.get("background") or {}
    if not isinstance(background, dict):
        raise ExtensionManifestError("background must be an object")
    background_scripts = tuple(
        _safe_relative_path(root, script)
        for script in _strings(background.get("scripts"), "background.scripts")
    )
    worker_raw = background.get("service_worker")
    if worker_raw is not None and not isinstance(worker_raw, str):
        raise ExtensionManifestError("background.service_worker must be a path")
    service_worker = _safe_relative_path(root, worker_raw) if worker_raw else None
    if manifest_version == 3 and background_scripts:
        raise ExtensionManifestError("Manifest V3 background pages must use service_worker")
    if manifest_version == 2 and service_worker:
        raise ExtensionManifestError("Manifest V2 does not support background.service_worker")

    action_raw = payload.get("action", payload.get("browser_action", payload.get("page_action", {})))
    if action_raw is None:
        action_raw = {}
    if not isinstance(action_raw, dict):
        raise ExtensionManifestError("action/browser_action must be an object")
    action = dict(action_raw)
    popup = action.get("default_popup")
    if popup:
        if not isinstance(popup, str):
            raise ExtensionManifestError("action.default_popup must be a path")
        action["default_popup"] = _safe_relative_path(root, popup)

    icons_raw = payload.get("icons", {})
    if not isinstance(icons_raw, dict):
        raise ExtensionManifestError("icons must be an object")
    icons: dict[int, str] = {}
    for size, icon_path in icons_raw.items():
        try:
            numeric_size = int(size)
        except (TypeError, ValueError) as exc:
            raise ExtensionManifestError(f"Invalid icon size: {size!r}") from exc
        if not isinstance(icon_path, str):
            raise ExtensionManifestError("Icon path must be a string")
        icons[numeric_size] = _safe_relative_path(root, icon_path)

    return ExtensionManifest(
        manifest_version=manifest_version,
        name=name,
        version=version,
        description=description,
        permissions=frozenset(api_permissions),
        host_permissions=frozenset(host_permissions),
        optional_permissions=frozenset(_strings(payload.get("optional_permissions"), "optional_permissions")),
        content_scripts=content_scripts,
        background_scripts=background_scripts,
        background_service_worker=service_worker,
        action=action,
        icons=icons,
        minimum_chrome_version=(
            str(payload["minimum_chrome_version"]) if payload.get("minimum_chrome_version") else None
        ),
        raw=payload,
    )


def _extension_id(root: Path, manifest: ExtensionManifest) -> str:
    key = manifest.raw.get("key")
    identity = str(key) if key else str(root.resolve()).casefold()
    # 32 lowercase characters fit Chrome's extension-id-sized UI affordances.
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _match_pattern(pattern: str, url: str) -> bool:
    if pattern == "<all_urls>":
        return urlsplit(url).scheme in {"http", "https", "file", "ftp"}
    match = re.fullmatch(r"(\*|http|https|file|ftp)://([^/]*)(/.*)", pattern)
    if match is None:
        return False
    parsed = urlsplit(url)
    scheme, host_pattern, path_pattern = match.groups()
    if scheme == "*":
        if parsed.scheme not in {"http", "https"}:
            return False
    elif parsed.scheme != scheme:
        return False
    host = (parsed.hostname or "").lower()
    host_pattern = host_pattern.lower()
    if host_pattern == "*":
        host_matches = True
    elif host_pattern.startswith("*."):
        base = host_pattern[2:]
        host_matches = host == base or host.endswith("." + base)
    else:
        host_matches = host == host_pattern
    return host_matches and fnmatch.fnmatchcase(parsed.path or "/", path_pattern)


class ExtensionManager:
    """Installs, discovers, enables and validates unpacked extensions."""

    def __init__(
        self,
        extensions_root: str | Path,
        *,
        runtime: ExtensionRuntimeAdapter | None = None,
    ) -> None:
        self.extensions_root = Path(extensions_root).expanduser().resolve()
        self.extensions_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.extensions_root / "extensions-state.json"
        self.runtime = runtime
        self._lock = threading.RLock()
        self._extensions: dict[str, Extension] = {}
        self._state = self._load_state()
        self.refresh()

    def list_extensions(self, *, enabled_only: bool = False) -> tuple[Extension, ...]:
        with self._lock:
            values = self._extensions.values()
            if enabled_only:
                values = (extension for extension in values if extension.enabled)
            return tuple(sorted(values, key=lambda item: (item.manifest.name.casefold(), item.id)))

    def get(self, extension_id: str) -> Extension:
        with self._lock:
            try:
                return self._extensions[extension_id]
            except KeyError as exc:
                raise KeyError(f"Unknown extension: {extension_id}") from exc

    def refresh(self) -> tuple[Extension, ...]:
        """Reload managed directories and registered external unpacked locations."""

        with self._lock:
            candidates: dict[str, tuple[Path, bool]] = {}
            for child in self.extensions_root.iterdir():
                if child.is_dir() and (child / "manifest.json").is_file():
                    candidates[child.name] = (child, True)
            for extension_id, item in self._state.get("extensions", {}).items():
                location = item.get("location")
                if location:
                    candidates.setdefault(extension_id, (Path(location), bool(item.get("managed", False))))

            loaded: dict[str, Extension] = {}
            for hinted_id, (root, managed) in candidates.items():
                try:
                    manifest = load_manifest(root)
                    extension_id = hinted_id if managed else _extension_id(root, manifest)
                    state = self._state.get("extensions", {}).get(extension_id, {})
                    installed_raw = state.get("installed_at")
                    installed_at = (
                        datetime.fromisoformat(installed_raw) if installed_raw else datetime.now(UTC)
                    )
                    if installed_at.tzinfo is None:
                        installed_at = installed_at.replace(tzinfo=UTC)
                    loaded[extension_id] = Extension(
                        extension_id=extension_id,
                        root=root.resolve(),
                        manifest=manifest,
                        enabled=bool(state.get("enabled", True)),
                        installed_at=installed_at.astimezone(UTC),
                        managed=managed,
                    )
                except (ExtensionError, OSError, ValueError):
                    logger.exception("Could not load extension from %s", root)
            self._extensions = loaded
            self._persist_extensions()
            if self.runtime is not None:
                for extension in loaded.values():
                    if extension.enabled:
                        try:
                            self.runtime.activate(extension)
                        except Exception:
                            logger.exception("Extension runtime failed to activate %s", extension.id)
            return self.list_extensions()

    def install_unpacked(
        self,
        source: str | Path,
        *,
        copy_into_profile: bool = True,
        enabled: bool = True,
    ) -> Extension:
        source_path = Path(source).expanduser().resolve()
        manifest = load_manifest(source_path)
        extension_id = _extension_id(source_path, manifest)
        with self._lock:
            if extension_id in self._extensions:
                raise ExtensionError(f"Extension is already installed: {extension_id}")
            installed_at = datetime.now(UTC)
            managed = copy_into_profile
            target = source_path
            if copy_into_profile:
                target = self.extensions_root / extension_id
                if target.exists():
                    raise ExtensionError(f"Extension directory already exists: {target}")
                temporary = Path(tempfile.mkdtemp(prefix="extension-", dir=self.extensions_root))
                try:
                    shutil.copytree(source_path, temporary / "payload")
                    # Validate the copy before making it visible in the registry.
                    manifest = load_manifest(temporary / "payload")
                    os.replace(temporary / "payload", target)
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)
            extension = Extension(
                extension_id=extension_id,
                root=target,
                manifest=manifest,
                enabled=enabled,
                installed_at=installed_at,
                managed=managed,
            )
            self._extensions[extension_id] = extension
            self._persist_extensions()
            if enabled and self.runtime is not None:
                self.runtime.activate(extension)
            return extension

    def install_zip(self, archive: str | Path, *, enabled: bool = True) -> Extension:
        """Safely install a ZIP containing an unpacked extension (not a CRX)."""

        archive_path = Path(archive).expanduser().resolve()
        temporary = Path(tempfile.mkdtemp(prefix="auralis-extension-"))
        try:
            with zipfile.ZipFile(archive_path) as bundle:
                root = temporary.resolve()
                for member in bundle.infolist():
                    destination = (temporary / member.filename).resolve()
                    try:
                        destination.relative_to(root)
                    except ValueError as exc:
                        raise ExtensionError("Extension archive contains an unsafe path") from exc
                bundle.extractall(temporary)
            extension_root = temporary
            if not (extension_root / "manifest.json").is_file():
                children = [child for child in temporary.iterdir() if child.is_dir()]
                if len(children) == 1 and (children[0] / "manifest.json").is_file():
                    extension_root = children[0]
            return self.install_unpacked(extension_root, copy_into_profile=True, enabled=enabled)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def load_unpacked(self, source: str | Path, *, enabled: bool = True) -> Extension:
        """Register a development extension in place without copying its files."""

        return self.install_unpacked(source, copy_into_profile=False, enabled=enabled)

    load_extension = load_unpacked

    def set_enabled(self, extension_id: str, enabled: bool) -> Extension:
        with self._lock:
            current = self.get(extension_id)
            if current.enabled == enabled:
                return current
            updated = Extension(
                extension_id=current.id,
                root=current.root,
                manifest=current.manifest,
                enabled=enabled,
                installed_at=current.installed_at,
                managed=current.managed,
            )
            self._extensions[extension_id] = updated
            self._persist_extensions()
            if self.runtime is not None:
                if enabled:
                    self.runtime.activate(updated)
                else:
                    self.runtime.deactivate(extension_id)
            return updated

    def uninstall(self, extension_id: str, *, remove_files: bool = True) -> None:
        with self._lock:
            extension = self.get(extension_id)
            if self.runtime is not None and extension.enabled:
                self.runtime.deactivate(extension_id)
            del self._extensions[extension_id]
            self._persist_extensions()
            if remove_files and extension.managed and extension.root.exists():
                resolved = extension.root.resolve()
                if resolved.parent != self.extensions_root:
                    raise ExtensionError("Refusing to remove an extension outside the profile")
                shutil.rmtree(resolved)

    def content_scripts_for(
        self, url: str, *, run_at: str | None = None
    ) -> tuple[tuple[Extension, ContentScript], ...]:
        matches: list[tuple[Extension, ContentScript]] = []
        with self._lock:
            for extension in self._extensions.values():
                if not extension.enabled:
                    continue
                for script in extension.manifest.content_scripts:
                    if (run_at is None or script.run_at == run_at) and script.matches_url(url):
                        matches.append((extension, script))
        return tuple(matches)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "extensions": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("extensions"), dict):
                raise ValueError("Unsupported extension state version")
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not load extension state from %s", self.state_path)
            return {"version": 1, "extensions": {}}

    def _persist_extensions(self) -> None:
        self._state = {
            "version": 1,
            "extensions": {
                extension.id: {
                    "enabled": extension.enabled,
                    "location": str(extension.root),
                    "managed": extension.managed,
                    "installed_at": extension.installed_at.isoformat(),
                }
                for extension in self._extensions.values()
            },
        }
        fd, temporary = tempfile.mkstemp(prefix="extensions-", suffix=".tmp", dir=self.extensions_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


# Product-facing synonym used in settings routes.
ExtensionRegistry = ExtensionManager
