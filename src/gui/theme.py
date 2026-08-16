"""Colour schemes for the window chrome and the plot canvases.

One entry per theme in :data:`THEMES` — adding another (a dark kiosk scheme, say)
is a table entry plus a config enum value, no panel changes.

Two things have to happen in the right order: pyqtgraph's background/foreground
are *global config options* read when a plot widget is constructed, so
:func:`apply` must run before any panel is built. ``MainWindow`` does that at the
top of its constructor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from config.schema import AppTheme

_log = logging.getLogger(__name__)

__all__ = ["Theme", "resolve", "apply"]


@dataclass(frozen=True, slots=True)
class Theme:
    """Everything the panels need to colour themselves consistently."""

    name: AppTheme
    plot_background: str
    plot_foreground: str
    grid_alpha: float
    label_color: str
    """Sensor labels drawn on top of the temperature field."""
    label_fill: tuple[int, int, int, int]
    """Backing plate behind those labels. The colour map runs from near-black to
    near-white, so no single text colour stays readable over the whole field."""
    warn_color: str
    """Out-of-range readings, in both panels."""
    marker_pen: str
    marker_brush: tuple[int, int, int, int]
    swatch_border: str
    use_fusion_style: bool
    """Native styles ignore most palette colours; Fusion honours them."""
    palette: tuple[tuple[QPalette.ColorRole, str], ...] = ()
    disabled_palette: tuple[tuple[QPalette.ColorRole, str], ...] = ()


#: The look the app has always had: the system's own window colours (grey on
#: Windows and Raspberry Pi OS) with dark plot canvases.
_GRAY = Theme(
    name=AppTheme.GRAY,
    plot_background="k",
    plot_foreground="d",
    grid_alpha=0.3,
    label_color="#f5f5f5",
    label_fill=(0, 0, 0, 130),
    warn_color="#ff6b6b",
    marker_pen="#101010",
    marker_brush=(255, 255, 255, 200),
    swatch_border="#333333",
    use_fusion_style=False,
)

#: White window chrome *and* white plot canvases, for bright rooms and print-like
#: screenshots.
_WHITE = Theme(
    name=AppTheme.WHITE,
    plot_background="w",
    plot_foreground="#1a1a1a",
    grid_alpha=0.25,
    label_color="#1a1a1a",
    label_fill=(255, 255, 255, 200),
    warn_color="#c62828",
    marker_pen="#1a1a1a",
    marker_brush=(255, 255, 255, 235),
    swatch_border="#9e9e9e",
    use_fusion_style=True,
    palette=(
        (QPalette.ColorRole.Window, "#ffffff"),
        (QPalette.ColorRole.WindowText, "#1a1a1a"),
        (QPalette.ColorRole.Base, "#ffffff"),
        (QPalette.ColorRole.AlternateBase, "#f4f4f4"),
        (QPalette.ColorRole.Text, "#1a1a1a"),
        (QPalette.ColorRole.Button, "#f7f7f7"),
        (QPalette.ColorRole.ButtonText, "#1a1a1a"),
        (QPalette.ColorRole.ToolTipBase, "#ffffff"),
        (QPalette.ColorRole.ToolTipText, "#1a1a1a"),
        (QPalette.ColorRole.Highlight, "#4e79a7"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
        (QPalette.ColorRole.Light, "#ffffff"),
        (QPalette.ColorRole.Mid, "#cfcfcf"),
        (QPalette.ColorRole.Dark, "#9e9e9e"),
    ),
    disabled_palette=(
        (QPalette.ColorRole.WindowText, "#9e9e9e"),
        (QPalette.ColorRole.Text, "#9e9e9e"),
        (QPalette.ColorRole.ButtonText, "#9e9e9e"),
    ),
)

THEMES: dict[AppTheme, Theme] = {AppTheme.GRAY: _GRAY, AppTheme.WHITE: _WHITE}


def resolve(name: AppTheme) -> Theme:
    """Look up a theme; unknown names fall back to the default rather than failing."""
    theme = THEMES.get(name)
    if theme is None:
        _log.warning("No palette defined for theme %r; using %s", name, AppTheme.GRAY.value)
        return _GRAY
    return theme


def apply(theme: Theme, app: QApplication | None = None) -> Theme:
    """Apply `theme` globally. Must be called before any plot widget is created."""
    pg.setConfigOption("background", theme.plot_background)
    pg.setConfigOption("foreground", theme.plot_foreground)

    app = app or QApplication.instance()
    if app is None:
        return theme

    if theme.use_fusion_style:
        app.setStyle("Fusion")
    if theme.palette:
        palette = QPalette()
        for role, color in theme.palette:
            palette.setColor(role, QColor(color))
        for role, color in theme.disabled_palette:
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color))
        app.setPalette(palette)

    _log.debug("Applied %s theme", theme.name.value)
    return theme
