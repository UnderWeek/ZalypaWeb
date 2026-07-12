from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from browser.core.tabs import TabManager


def test_tab_session_and_closed_restore(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    path = tmp_path / "session.json"
    manager = TabManager(path)
    pinned = manager.add("https://pinned.example", pinned=True)
    group = manager.create_group("Исследование", "#006A6A")
    regular = manager.add("https://example.com", group_id=group.id)
    manager.update(regular.id, title="Example", zoom=1.25)
    manager.set_current(regular.id)
    assert manager.save_session()

    restored = TabManager(path)
    assert restored.load_session()
    assert [tab.id for tab in restored.tabs] == [pinned.id, regular.id]
    assert restored.current_id == regular.id
    assert restored.get(regular.id).zoom == 1.25

    tail = restored.add("https://tail.example")
    restored.update(tail.id, pinned=True)
    assert [tab.id for tab in restored.tabs[:2]] == [pinned.id, tail.id]
    restored.update(tail.id, pinned=False)
    assert [tab.id for tab in restored.tabs[:3]] == [pinned.id, tail.id, regular.id]

    restored.remove(regular.id)
    reopened = restored.restore_closed()
    assert reopened is not None
    assert reopened.url == "https://example.com"
