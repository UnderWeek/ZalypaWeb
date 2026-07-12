"""Material 3 inspired design system used by Auralis Browser.

The module intentionally has no dependency on the browser core.  Widgets can be
styled by calling :class:`ThemeManager` once at application startup and by using
the semantic ``materialRole`` dynamic property where a specialised component is
not necessary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Final

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_ACCENT: Final[str] = "#6750A4"


class ThemeMode(str, Enum):
    """Supported colour modes."""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class Density(str, Enum):
    """Global interface spacing presets."""

    COMPACT = "compact"
    COMFORTABLE = "comfortable"
    SPACIOUS = "spacious"


@dataclass(frozen=True, slots=True)
class MaterialPalette:
    """Semantic Material colour roles.

    Colours are represented as ``#RRGGBB`` strings so the palette can be
    serialised directly to profile settings and inserted into QSS safely.
    """

    primary: str
    on_primary: str
    primary_container: str
    on_primary_container: str
    secondary: str
    on_secondary: str
    secondary_container: str
    on_secondary_container: str
    tertiary: str
    on_tertiary: str
    error: str
    on_error: str
    error_container: str
    on_error_container: str
    background: str
    on_background: str
    surface: str
    on_surface: str
    surface_variant: str
    on_surface_variant: str
    surface_container_low: str
    surface_container: str
    surface_container_high: str
    outline: str
    outline_variant: str
    inverse_surface: str
    inverse_on_surface: str
    scrim: str
    shadow: str
    is_dark: bool


def _as_color(value: str | QColor) -> QColor:
    color = QColor(value)
    return color if color.isValid() else QColor(DEFAULT_ACCENT)


def _hex(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb).upper()


def _mix(first: str | QColor, second: str | QColor, amount: float) -> str:
    """Blend two colours in linear channel space."""

    a = _as_color(first)
    b = _as_color(second)
    amount = max(0.0, min(1.0, amount))
    return _hex(
        QColor(
            round(a.red() + (b.red() - a.red()) * amount),
            round(a.green() + (b.green() - a.green()) * amount),
            round(a.blue() + (b.blue() - a.blue()) * amount),
        )
    )


def _rotate_hue(color: str | QColor, degrees: float, saturation_scale: float = 1.0) -> str:
    source = _as_color(color)
    hue, saturation, lightness, alpha = source.getHslF()
    if hue < 0:
        hue = 0.0
    result = QColor.fromHslF(
        (hue + degrees / 360.0) % 1.0,
        max(0.0, min(1.0, saturation * saturation_scale)),
        lightness,
        alpha,
    )
    return _hex(result)


def build_palette(accent: str | QColor = DEFAULT_ACCENT, dark: bool = False) -> MaterialPalette:
    """Create a cohesive light or dark palette from one user accent colour.

    It is not a byte-for-byte implementation of Google's HCT algorithm.  The
    generated tones follow the same semantic contrast model while remaining
    deterministic and lightweight for desktop profiles.
    """

    seed = _hex(_as_color(accent))
    white, black = "#FFFFFF", "#000000"
    secondary_seed = _rotate_hue(seed, 18, 0.48)
    tertiary_seed = _rotate_hue(seed, 58, 0.72)

    if dark:
        return MaterialPalette(
            primary=_mix(seed, white, 0.58),
            on_primary=_mix(seed, black, 0.60),
            primary_container=_mix(seed, black, 0.42),
            on_primary_container=_mix(seed, white, 0.82),
            secondary=_mix(secondary_seed, white, 0.60),
            on_secondary=_mix(secondary_seed, black, 0.62),
            secondary_container=_mix(secondary_seed, black, 0.43),
            on_secondary_container=_mix(secondary_seed, white, 0.82),
            tertiary=_mix(tertiary_seed, white, 0.55),
            on_tertiary=_mix(tertiary_seed, black, 0.64),
            error="#FFB4AB",
            on_error="#690005",
            error_container="#93000A",
            on_error_container="#FFDAD6",
            background=_mix(seed, "#111116", 0.08),
            on_background="#E7E1E9",
            surface=_mix(seed, "#121217", 0.07),
            on_surface="#E7E1E9",
            surface_variant=_mix(seed, "#2B2930", 0.13),
            on_surface_variant="#CAC4D0",
            surface_container_low=_mix(seed, "#1D1B20", 0.06),
            surface_container=_mix(seed, "#211F26", 0.08),
            surface_container_high=_mix(seed, "#2B2930", 0.08),
            outline="#938F99",
            outline_variant="#49454F",
            inverse_surface="#E7E1E9",
            inverse_on_surface="#322F35",
            scrim="#000000",
            shadow="#000000",
            is_dark=True,
        )

    return MaterialPalette(
        primary=_mix(seed, black, 0.10),
        on_primary=white,
        primary_container=_mix(seed, white, 0.77),
        on_primary_container=_mix(seed, black, 0.68),
        secondary=_mix(secondary_seed, black, 0.25),
        on_secondary=white,
        secondary_container=_mix(secondary_seed, white, 0.79),
        on_secondary_container=_mix(secondary_seed, black, 0.70),
        tertiary=_mix(tertiary_seed, black, 0.22),
        on_tertiary=white,
        error="#BA1A1A",
        on_error=white,
        error_container="#FFDAD6",
        on_error_container="#410002",
        background=_mix(seed, "#FFFBFE", 0.025),
        on_background="#1D1B20",
        surface=_mix(seed, "#FFFBFE", 0.018),
        on_surface="#1D1B20",
        surface_variant=_mix(seed, "#E7E0EC", 0.055),
        on_surface_variant="#49454F",
        surface_container_low=_mix(seed, "#F7F2FA", 0.025),
        surface_container=_mix(seed, "#F3EDF7", 0.03),
        surface_container_high=_mix(seed, "#ECE6F0", 0.04),
        outline="#79747E",
        outline_variant="#CAC4D0",
        inverse_surface="#322F35",
        inverse_on_surface="#F5EFF7",
        scrim="#000000",
        shadow="#000000",
        is_dark=False,
    )


def system_prefers_dark() -> bool:
    """Return the desktop colour preference when supported by this Qt build."""

    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        scheme = app.styleHints().colorScheme()
        color_scheme = getattr(Qt, "ColorScheme", None)
        return bool(color_scheme and scheme == color_scheme.Dark)
    except (AttributeError, RuntimeError):
        palette = app.palette()
        return palette.color(QPalette.ColorRole.Window).lightness() < 128


def material_font() -> QFont:
    """Pick a variable/modern sans font available on the host system."""

    available = set(QFontDatabase.families())
    for family in ("Roboto Flex", "Roboto", "Segoe UI Variable", "Segoe UI", "Noto Sans"):
        if family in available:
            font = QFont(family)
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            font.setPointSizeF(10.0)
            return font
    font = QFont()
    font.setPointSizeF(10.0)
    return font


def build_stylesheet(
    palette: MaterialPalette,
    density: Density | str = Density.COMFORTABLE,
) -> str:
    """Build the complete semantic QSS stylesheet."""

    try:
        density = Density(density)
    except ValueError:
        density = Density.COMFORTABLE
    metrics = {
        Density.COMPACT: (6, 10, 32, 10),
        Density.COMFORTABLE: (8, 14, 40, 12),
        Density.SPACIOUS: (11, 18, 48, 14),
    }
    vpad, hpad, control_height, radius = metrics[density]
    p = palette
    check_mark = (
        "image: none;"  # Indicator colour is still visible without external assets.
    )
    return f"""
/* Auralis Material 3 base */
QWidget {{
    color: {p.on_surface};
    background-color: {p.background};
    selection-background-color: {p.primary_container};
    selection-color: {p.on_primary_container};
    font-size: 10pt;
}}
QWidget:disabled {{ color: {p.outline}; }}
QWidget[materialRole="transparent"], QFrame[materialRole="transparent"] {{
    background: transparent;
}}
QWidget[materialRole="surface"], QFrame[materialRole="surface"] {{
    background: {p.surface};
    border: 1px solid {p.outline_variant};
    border-radius: {radius + 4}px;
}}
QWidget[materialRole="surfaceContainer"], QFrame[materialRole="surfaceContainer"] {{
    background: {p.surface_container};
    border: none;
    border-radius: {radius + 6}px;
}}
QWidget[materialRole="surfaceContainerHigh"], QFrame[materialRole="surfaceContainerHigh"] {{
    background: {p.surface_container_high};
    border: none;
    border-radius: {radius + 6}px;
}}
QWidget[materialRole="primaryContainer"], QFrame[materialRole="primaryContainer"] {{
    color: {p.on_primary_container};
    background: {p.primary_container};
    border: none;
    border-radius: {radius + 6}px;
}}
QLabel {{ background: transparent; }}
QLabel[materialRole="display"] {{ font-size: 27pt; font-weight: 600; color: {p.on_background}; }}
QLabel[materialRole="headline"] {{ font-size: 20pt; font-weight: 600; color: {p.on_surface}; }}
QLabel[materialRole="title"] {{ font-size: 14pt; font-weight: 600; color: {p.on_surface}; }}
QLabel[materialRole="subtitle"] {{ font-size: 11pt; font-weight: 600; color: {p.on_surface}; }}
QLabel[materialRole="body"] {{ font-size: 10pt; color: {p.on_surface_variant}; }}
QLabel[materialRole="label"] {{ font-size: 9pt; font-weight: 600; color: {p.on_surface_variant}; }}
QLabel[materialRole="error"] {{ color: {p.error}; }}

QPushButton, QToolButton {{
    min-height: {control_height}px;
    padding: 0 {hpad}px;
    border: none;
    border-radius: {control_height // 2}px;
    color: {p.on_surface};
    background: transparent;
    font-weight: 600;
}}
QPushButton:hover, QToolButton:hover {{ background: {p.surface_variant}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {p.outline_variant}; }}
QPushButton:checked, QToolButton:checked {{ color: {p.on_secondary_container}; background: {p.secondary_container}; }}
QPushButton[materialRole="filled"], QToolButton[materialRole="filled"] {{
    color: {p.on_primary}; background: {p.primary}; padding: 0 {hpad + 4}px;
}}
QPushButton[materialRole="filled"]:hover, QToolButton[materialRole="filled"]:hover {{
    background: {_mix(p.primary, p.on_primary, 0.12)};
}}
QPushButton[materialRole="tonal"], QToolButton[materialRole="tonal"] {{
    color: {p.on_secondary_container}; background: {p.secondary_container}; padding: 0 {hpad + 4}px;
}}
QPushButton[materialRole="outlined"], QToolButton[materialRole="outlined"] {{
    color: {p.primary}; background: transparent; border: 1px solid {p.outline}; padding: 0 {hpad + 4}px;
}}
QPushButton[materialRole="danger"] {{ color: {p.on_error}; background: {p.error}; }}
QPushButton[materialRole="fab"], QToolButton[materialRole="fab"] {{
    min-width: 52px; min-height: 52px; max-width: 52px; max-height: 52px;
    border-radius: 18px; color: {p.on_primary_container}; background: {p.primary_container};
    font-size: 18pt;
}}
QToolButton[materialRole="icon"] {{
    min-width: {control_height}px; max-width: {control_height}px;
    min-height: {control_height}px; max-height: {control_height}px;
    padding: 0; border-radius: {control_height // 2}px;
}}

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    color: {p.on_surface}; background: {p.surface_container_high};
    border: 1px solid transparent; border-radius: {radius}px;
    padding: {vpad}px {hpad}px; min-height: {max(18, control_height - 2 * vpad)}px;
}}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border-color: {p.outline_variant};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {p.primary}; padding: {max(0, vpad - 1)}px {max(0, hpad - 1)}px;
}}
QLineEdit[materialRole="search"] {{
    border-radius: {control_height // 2 + 7}px; padding-left: {hpad + 8}px;
    background: {p.surface_container_high}; min-height: {control_height + 10}px;
}}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox QAbstractItemView {{
    color: {p.on_surface}; background: {p.surface_container}; border: 1px solid {p.outline_variant};
    border-radius: {radius}px; padding: 6px; outline: 0;
}}
QComboBox QAbstractItemView::item {{ min-height: 34px; padding: 3px 8px; border-radius: 7px; }}
QComboBox QAbstractItemView::item:selected {{ color: {p.on_secondary_container}; background: {p.secondary_container}; }}

QCheckBox, QRadioButton {{ spacing: 10px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 18px; height: 18px; border: 2px solid {p.outline}; background: transparent;
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 10px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.primary}; background: {p.primary_container}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    border-color: {p.primary}; background: {p.primary}; {check_mark}
}}

QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {p.surface_variant}; }}
QSlider::sub-page:horizontal {{ border-radius: 2px; background: {p.primary}; }}
QSlider::handle:horizontal {{
    width: 20px; height: 20px; margin: -8px 0; border-radius: 10px;
    background: {p.primary}; border: 3px solid {p.primary_container};
}}
QProgressBar {{
    min-height: 6px; max-height: 6px; border: none; border-radius: 3px;
    background: {p.surface_variant}; color: transparent;
}}
QProgressBar::chunk {{ border-radius: 3px; background: {p.primary}; }}

QScrollArea, QAbstractScrollArea, QAbstractItemView {{ border: none; background: transparent; outline: 0; }}
QListView::item, QListWidget::item, QTreeView::item, QTreeWidget::item {{
    min-height: {control_height}px; border-radius: {radius}px; padding: 2px 8px;
}}
QListView::item:hover, QListWidget::item:hover, QTreeView::item:hover, QTreeWidget::item:hover {{
    background: {p.surface_variant};
}}
QListView::item:selected, QListWidget::item:selected, QTreeView::item:selected, QTreeWidget::item:selected {{
    color: {p.on_secondary_container}; background: {p.secondary_container};
}}
QHeaderView::section {{
    color: {p.on_surface_variant}; background: {p.surface_container}; border: none;
    border-bottom: 1px solid {p.outline_variant}; padding: {vpad}px {hpad}px; font-weight: 600;
}}
QScrollBar:vertical {{ width: 12px; background: transparent; margin: 3px; }}
QScrollBar:horizontal {{ height: 12px; background: transparent; margin: 3px; }}
QScrollBar::handle {{ background: {p.outline_variant}; border-radius: 4px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {p.outline}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; }}

QTabWidget::pane {{ border: none; background: {p.background}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    color: {p.on_surface_variant}; background: {p.surface_container_low}; border: none;
    border-radius: {radius}px; min-width: 92px; max-width: 240px;
    min-height: {control_height}px; padding: 0 {hpad}px; margin: 3px 2px;
}}
QTabBar::tab:hover {{ color: {p.on_surface}; background: {p.surface_variant}; }}
QTabBar::tab:selected {{ color: {p.on_secondary_container}; background: {p.secondary_container}; }}
QTabBar::close-button {{ width: 18px; height: 18px; border-radius: 9px; }}

QMenu {{
    color: {p.on_surface}; background: {p.surface_container}; border: 1px solid {p.outline_variant};
    border-radius: {radius + 2}px; padding: 6px;
}}
QMenu::item {{ min-height: 32px; padding: 4px 28px 4px 12px; border-radius: {radius - 2}px; }}
QMenu::item:selected {{ color: {p.on_secondary_container}; background: {p.secondary_container}; }}
QMenu::separator {{ height: 1px; background: {p.outline_variant}; margin: 5px 10px; }}
QToolTip {{
    color: {p.inverse_on_surface}; background: {p.inverse_surface}; border: none;
    border-radius: 6px; padding: 6px 9px;
}}
QDialog {{ background: {p.surface}; }}
QSplitter::handle {{ background: {p.outline_variant}; width: 1px; height: 1px; }}
"""


class ThemeManager(QObject):
    """Owns the active palette and applies it to an app or widget tree."""

    themeChanged = Signal(object)
    modeChanged = Signal(str)
    accentChanged = Signal(str)
    densityChanged = Signal(str)

    def __init__(
        self,
        app: QApplication | None = None,
        mode: ThemeMode | str = ThemeMode.SYSTEM,
        accent: str = DEFAULT_ACCENT,
        density: Density | str = Density.COMFORTABLE,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app or QApplication.instance()
        self._mode = self._coerce_mode(mode)
        self._accent = _hex(_as_color(accent))
        self._density = self._coerce_density(density)
        self._palette = build_palette(self._accent, self.is_dark)

    @staticmethod
    def _coerce_mode(mode: ThemeMode | str) -> ThemeMode:
        try:
            return ThemeMode(mode)
        except ValueError:
            return ThemeMode.SYSTEM

    @staticmethod
    def _coerce_density(density: Density | str) -> Density:
        try:
            return Density(density)
        except ValueError:
            return Density.COMFORTABLE

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def accent(self) -> str:
        return self._accent

    @property
    def density(self) -> Density:
        return self._density

    @property
    def palette(self) -> MaterialPalette:
        return self._palette

    @property
    def is_dark(self) -> bool:
        return self._mode is ThemeMode.DARK or (
            self._mode is ThemeMode.SYSTEM and system_prefers_dark()
        )

    def set_mode(self, mode: ThemeMode | str) -> None:
        value = self._coerce_mode(mode)
        if value == self._mode:
            return
        self._mode = value
        self.modeChanged.emit(value.value)
        self.apply()

    def set_accent(self, accent: str | QColor) -> None:
        value = _hex(_as_color(accent))
        if value == self._accent:
            return
        self._accent = value
        self.accentChanged.emit(value)
        self.apply()

    def set_density(self, density: Density | str) -> None:
        value = self._coerce_density(density)
        if value == self._density:
            return
        self._density = value
        self.densityChanged.emit(value.value)
        self.apply()

    def toggle_theme(self) -> None:
        self.set_mode(ThemeMode.LIGHT if self.is_dark else ThemeMode.DARK)

    def apply(self, target: QApplication | QWidget | None = None) -> MaterialPalette:
        """Apply the current theme and return the generated semantic palette."""

        self._palette = build_palette(self._accent, self.is_dark)
        target = target or self._app or QApplication.instance()
        if target is None:
            LOGGER.debug("Theme prepared before QApplication was created")
            return self._palette

        if isinstance(target, QApplication):
            self._app = target
            target.setFont(material_font())
            target.setPalette(self._qt_palette(self._palette))
        target.setStyleSheet(build_stylesheet(self._palette, self._density))
        self.themeChanged.emit(self._palette)
        return self._palette

    @staticmethod
    def _qt_palette(colors: MaterialPalette) -> QPalette:
        palette = QPalette()
        mapping = {
            QPalette.ColorRole.Window: colors.background,
            QPalette.ColorRole.WindowText: colors.on_background,
            QPalette.ColorRole.Base: colors.surface,
            QPalette.ColorRole.AlternateBase: colors.surface_container,
            QPalette.ColorRole.Text: colors.on_surface,
            QPalette.ColorRole.Button: colors.surface_container,
            QPalette.ColorRole.ButtonText: colors.on_surface,
            QPalette.ColorRole.Highlight: colors.primary_container,
            QPalette.ColorRole.HighlightedText: colors.on_primary_container,
            QPalette.ColorRole.ToolTipBase: colors.inverse_surface,
            QPalette.ColorRole.ToolTipText: colors.inverse_on_surface,
            QPalette.ColorRole.PlaceholderText: colors.outline,
            QPalette.ColorRole.Link: colors.primary,
        }
        for role, color in mapping.items():
            palette.setColor(role, QColor(color))
        return palette


class MaterialCard(QFrame):
    """Rounded semantic surface with optional elevation."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        role: str = "surfaceContainer",
        elevation: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setProperty("materialRole", role)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if elevation:
            set_elevation(self, elevation)


class MaterialButton(QPushButton):
    """Text button with a semantic Material variant."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        variant: str = "text",
    ) -> None:
        super().__init__(text, parent)
        self.setProperty("materialRole", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class MaterialIconButton(QToolButton):
    """Circular icon-only button."""

    def __init__(self, parent: QWidget | None = None, *, variant: str = "icon") -> None:
        super().__init__(parent)
        self.setProperty("materialRole", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)


def set_elevation(widget: QWidget, level: int = 1) -> QGraphicsDropShadowEffect:
    """Attach a soft Material-like shadow to ``widget``."""

    level = max(0, min(5, int(level)))
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius((0, 12, 20, 30, 42, 56)[level])
    shadow.setOffset(0, (0, 2, 4, 7, 10, 14)[level])
    shadow.setColor(QColor(0, 0, 0, (0, 42, 50, 58, 66, 74)[level]))
    widget.setGraphicsEffect(shadow)
    return shadow


def animate_opacity(widget: QWidget, start: float = 0.0, end: float = 1.0, duration: int = 180) -> QPropertyAnimation:
    """Animate a widget window opacity and keep the animation alive on it."""

    animation = QPropertyAnimation(widget, b"windowOpacity", widget)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setDuration(duration)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


# Backwards-friendly name for integrations that prefer a design-system noun.
MaterialTheme = ThemeManager


__all__ = [
    "DEFAULT_ACCENT",
    "Density",
    "MaterialButton",
    "MaterialCard",
    "MaterialIconButton",
    "MaterialPalette",
    "MaterialTheme",
    "ThemeManager",
    "ThemeMode",
    "animate_opacity",
    "build_palette",
    "build_stylesheet",
    "material_font",
    "set_elevation",
    "system_prefers_dark",
]
