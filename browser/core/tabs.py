"""Serializable tab and tab-group state.

The manager deliberately does not own ``QWebEngineView`` instances.  Keeping the
session model separate from Qt widgets makes crash recovery deterministic and
lets the UI recreate web processes when a profile is switched.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

LOGGER = logging.getLogger(__name__)

DEFAULT_NEW_TAB_URL = "auralis://newtab"
GROUP_COLORS = ("#6750A4", "#006A6A", "#7D5260", "#386A20", "#8C5000")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class TabState:
    """Persistent state for one browser tab."""

    id: str = field(default_factory=lambda: uuid4().hex)
    url: str = DEFAULT_NEW_TAB_URL
    title: str = "Новая вкладка"
    pinned: bool = False
    group_id: str | None = None
    muted: bool = False
    zoom: float = 1.0
    created_at: str = field(default_factory=_utc_now)
    last_active_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TabState":
        allowed = {name for name in cls.__dataclass_fields__}
        payload = {key: item for key, item in value.items() if key in allowed}
        payload["zoom"] = min(5.0, max(0.25, float(payload.get("zoom", 1.0))))
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TabGroup:
    """A named, colored collection of tabs."""

    id: str = field(default_factory=lambda: uuid4().hex)
    name: str = "Группа"
    color: str = GROUP_COLORS[0]
    collapsed: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TabGroup":
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: item for key, item in value.items() if key in allowed})


class TabManager(QObject):
    """Owns tab order and persists it as an atomic JSON session file."""

    tab_added = Signal(object, int)
    tab_removed = Signal(str, int)
    tab_updated = Signal(object, int)
    tab_moved = Signal(int, int)
    current_changed = Signal(str)
    groups_changed = Signal()
    session_saved = Signal()
    session_error = Signal(str)

    def __init__(
        self,
        session_path: str | Path,
        *,
        closed_limit: int = 30,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.session_path = Path(session_path)
        self._tabs: list[TabState] = []
        self._groups: dict[str, TabGroup] = {}
        self._closed: deque[tuple[TabState, int]] = deque(maxlen=closed_limit)
        self._current_id: str | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.save_session)

    @property
    def tabs(self) -> tuple[TabState, ...]:
        return tuple(self._tabs)

    @property
    def groups(self) -> tuple[TabGroup, ...]:
        return tuple(self._groups.values())

    @property
    def current_id(self) -> str | None:
        return self._current_id

    @property
    def current(self) -> TabState | None:
        return self.get(self._current_id) if self._current_id else None

    def get(self, tab_id: str | None) -> TabState | None:
        if tab_id is None:
            return None
        return next((tab for tab in self._tabs if tab.id == tab_id), None)

    def index_of(self, tab_id: str) -> int:
        return next((index for index, tab in enumerate(self._tabs) if tab.id == tab_id), -1)

    def add(
        self,
        url: str = DEFAULT_NEW_TAB_URL,
        *,
        title: str = "Новая вкладка",
        pinned: bool = False,
        group_id: str | None = None,
        index: int | None = None,
        make_current: bool = True,
        tab_id: str | None = None,
        zoom: float = 1.0,
    ) -> TabState:
        if group_id and group_id not in self._groups:
            group_id = None
        tab = TabState(
            id=tab_id or uuid4().hex,
            url=url or DEFAULT_NEW_TAB_URL,
            title=title or "Новая вкладка",
            pinned=pinned,
            group_id=group_id,
            zoom=zoom,
        )
        if index is None:
            index = self._default_insertion_index(pinned)
        index = max(0, min(index, len(self._tabs)))
        self._tabs.insert(index, tab)
        self.tab_added.emit(tab, index)
        if make_current or self._current_id is None:
            self.set_current(tab.id)
        self.schedule_save()
        return tab

    def remove(self, tab_id: str, *, remember: bool = True) -> TabState | None:
        index = self.index_of(tab_id)
        if index < 0:
            return None
        removed = self._tabs.pop(index)
        if remember:
            self._closed.appendleft((removed, index))
        self.tab_removed.emit(removed.id, index)
        if self._current_id == removed.id:
            if self._tabs:
                replacement = self._tabs[min(index, len(self._tabs) - 1)]
                self.set_current(replacement.id)
            else:
                self._current_id = None
        self.schedule_save()
        return removed

    def restore_closed(self) -> TabState | None:
        if not self._closed:
            return None
        previous, index = self._closed.popleft()
        return self.add(
            previous.url,
            title=previous.title,
            pinned=previous.pinned,
            group_id=previous.group_id,
            index=index,
            zoom=previous.zoom,
        )

    def update(self, tab_id: str, **changes: Any) -> TabState | None:
        tab = self.get(tab_id)
        if tab is None:
            return None
        editable = {"url", "title", "pinned", "group_id", "muted", "zoom", "last_active_at"}
        old_pinned = tab.pinned
        for key, value in changes.items():
            if key not in editable:
                continue
            if key == "zoom":
                value = min(5.0, max(0.25, float(value)))
            if key == "group_id" and value and value not in self._groups:
                value = None
            setattr(tab, key, value)
        if old_pinned != tab.pinned:
            old_index = self.index_of(tab_id)
            self._tabs.pop(old_index)
            new_index = self._default_insertion_index(tab.pinned)
            self._tabs.insert(new_index, tab)
            self.tab_moved.emit(old_index, new_index)
        index = self.index_of(tab_id)
        self.tab_updated.emit(tab, index)
        self.schedule_save()
        return tab

    def set_current(self, tab_id: str) -> bool:
        tab = self.get(tab_id)
        if tab is None:
            return False
        if self._current_id == tab_id:
            return True
        self._current_id = tab_id
        tab.last_active_at = _utc_now()
        self.current_changed.emit(tab_id)
        self.schedule_save()
        return True

    def move(self, source: int, destination: int) -> bool:
        if not (0 <= source < len(self._tabs)):
            return False
        destination = max(0, min(destination, len(self._tabs) - 1))
        tab = self._tabs[source]
        pinned_count = sum(item.pinned for item in self._tabs)
        if tab.pinned:
            destination = min(destination, max(0, pinned_count - 1))
        else:
            destination = max(destination, pinned_count)
        if source == destination:
            return True
        self._tabs.insert(destination, self._tabs.pop(source))
        self.tab_moved.emit(source, destination)
        self.schedule_save()
        return True

    def create_group(self, name: str, color: str | None = None) -> TabGroup:
        group = TabGroup(
            name=name.strip() or "Группа",
            color=color or GROUP_COLORS[len(self._groups) % len(GROUP_COLORS)],
        )
        self._groups[group.id] = group
        self.groups_changed.emit()
        self.schedule_save()
        return group

    def update_group(self, group_id: str, **changes: Any) -> TabGroup | None:
        group = self._groups.get(group_id)
        if group is None:
            return None
        for key in ("name", "color", "collapsed"):
            if key in changes:
                setattr(group, key, changes[key])
        self.groups_changed.emit()
        self.schedule_save()
        return group

    def remove_group(self, group_id: str, *, close_tabs: bool = False) -> None:
        if group_id not in self._groups:
            return
        if close_tabs:
            for tab in tuple(self._tabs):
                if tab.group_id == group_id:
                    self.remove(tab.id)
        else:
            for tab in self._tabs:
                if tab.group_id == group_id:
                    tab.group_id = None
                    self.tab_updated.emit(tab, self.index_of(tab.id))
        del self._groups[group_id]
        self.groups_changed.emit()
        self.schedule_save()

    def assign_group(self, tab_ids: Iterable[str], group_id: str | None) -> None:
        if group_id is not None and group_id not in self._groups:
            raise KeyError(f"Unknown tab group: {group_id}")
        for tab_id in tab_ids:
            self.update(tab_id, group_id=group_id)

    def schedule_save(self) -> None:
        self._save_timer.start()

    def save_session(self) -> bool:
        payload = {
            "version": 1,
            "current_id": self._current_id,
            "tabs": [tab.to_dict() for tab in self._tabs],
            "groups": [asdict(group) for group in self._groups.values()],
        }
        temporary = self.session_path.with_suffix(self.session_path.suffix + ".tmp")
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.session_path)
        except OSError as error:
            LOGGER.exception("Unable to save browser session")
            self.session_error.emit(str(error))
            return False
        self.session_saved.emit()
        return True

    def load_session(self) -> bool:
        if not self.session_path.exists():
            return False
        try:
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
            tabs = [TabState.from_dict(item) for item in payload.get("tabs", [])]
            groups = [TabGroup.from_dict(item) for item in payload.get("groups", [])]
        except (OSError, ValueError, TypeError) as error:
            LOGGER.exception("Unable to restore browser session")
            self.session_error.emit(str(error))
            return False
        unique_tabs: list[TabState] = []
        known_ids: set[str] = set()
        for tab in tabs:
            if tab.id in known_ids:
                tab.id = uuid4().hex
            known_ids.add(tab.id)
            unique_tabs.append(tab)
        self._tabs = unique_tabs
        self._groups = {group.id: group for group in groups}
        requested_current = payload.get("current_id")
        self._current_id = requested_current if self.get(requested_current) else None
        if self._current_id is None and self._tabs:
            self._current_id = self._tabs[0].id
        return bool(self._tabs)

    def clear(self) -> None:
        self._tabs.clear()
        self._groups.clear()
        self._closed.clear()
        self._current_id = None
        self.schedule_save()

    def _default_insertion_index(self, pinned: bool) -> int:
        pinned_count = sum(tab.pinned for tab in self._tabs)
        return pinned_count if pinned else len(self._tabs)

