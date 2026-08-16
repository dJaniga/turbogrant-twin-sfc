"""Colour helpers shared by the plotting panels.

Curve colours and the heatmap colour map both come from config; this module only
decides how to turn those settings into Qt/pyqtgraph objects, including sane
fallbacks when a colour map name is not available in the installed pyqtgraph.
"""

from __future__ import annotations

import logging
from typing import Sequence

import pyqtgraph as pg
from PySide6.QtGui import QColor

_log = logging.getLogger(__name__)

__all__ = ["curve_colors", "resolve_colormap"]

#: Ten visually distinct colours (Tableau-10 order), readable on the dark
#: pyqtgraph background. Used when `plotting.curve_colors` is null.
_DEFAULT_PALETTE = (
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1",
    "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#86bcb6",
)

#: Colour-map names tried in order when the configured one is unknown.
_COLORMAP_FALLBACKS = ("turbo", "viridis", "inferno", "CET-L16")


def curve_colors(count: int, configured: Sequence[str] | None = None) -> list[QColor]:
    """One distinct colour per sensor, in config order."""
    if configured:
        return [QColor(name) for name in configured[:count]]

    colors = [QColor(name) for name in _DEFAULT_PALETTE[:count]]
    # More sensors than palette entries: keep going around the hue wheel.
    for index in range(len(_DEFAULT_PALETTE), count):
        colors.append(pg.mkColor(pg.intColor(index, hues=max(count, 12))))
    return colors


def resolve_colormap(name: str) -> pg.ColorMap:
    """Look up a pyqtgraph colour map, falling back rather than failing to draw."""
    for candidate in (name, *_COLORMAP_FALLBACKS):
        for source in (None, "matplotlib", "colorcet"):
            try:
                colormap = pg.colormap.get(candidate, source=source, skipCache=False)
            except Exception:  # pyqtgraph raises bare exceptions for unknown names
                continue
            if colormap is not None:
                if candidate != name:
                    _log.warning(
                        "Colour map %r unavailable; using %r instead", name, candidate
                    )
                return colormap

    _log.warning("No colour map available for %r; using a grey ramp", name)
    return pg.ColorMap(pos=[0.0, 1.0], color=[(0, 0, 0, 255), (255, 255, 255, 255)])
