from __future__ import annotations

from PySide6.QtWidgets import QApplication

from browser.ui.material_theme import ThemeManager, build_palette
from browser.ui.navigation_bar import NavigationBar
from browser.ui.settings import DEFAULT_SETTINGS, SettingsPanel
from browser.ui.tabs_bar import MaterialTabBar


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_material_components_construct_and_exchange_state() -> None:
    app = _app()
    theme = ThemeManager(app, mode="dark", accent="#006A6A")
    palette = theme.apply()
    assert palette.is_dark
    theme.set_scale(115)
    assert theme.scale == 115
    assert build_palette("#6750A4", dark=False).primary.startswith("#")

    tabs = MaterialTabBar()
    moved: list[tuple[int, int]] = []
    tabs.tabMoved.connect(lambda source, destination: moved.append((source, destination)))
    first = tabs.add_tab("Первая", tab_id="first")
    second = tabs.add_tab("Вторая", tab_id="second", pinned=True)
    assert tabs.count() == 2
    assert tabs.tab_metadata(second).pinned
    assert moved == []  # Programmatic pin ordering is not reported as a user drag.
    first = tabs.index_for_id("first")
    tabs.update_tab(first, loading=True, group="Работа", group_color="#006A6A")

    third = tabs.add_tab("Третья", tab_id="third")
    tabs.native_bar.moveTab(third, 0)
    assert tabs.tab_metadata(0).pinned
    assert moved[-1][1] == 1

    navigation = NavigationBar()
    navigation.set_url("https://example.com")
    navigation.set_navigation_state(can_back=True, can_forward=False)
    navigation.set_progress(45)
    assert navigation.back_button.isEnabled()
    assert not navigation.forward_button.isEnabled()

    settings = SettingsPanel()
    settings.load_settings(DEFAULT_SETTINGS | {"appearance.theme": "dark"})
    assert settings.value("appearance.theme") == "dark"
