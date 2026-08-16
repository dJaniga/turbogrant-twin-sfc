"""Persistent sponsor branding area.

Deliberately *not* a QDockWidget: this must stay visible whatever the user does
to the dock layout (CLAUDE.md §3.2). ``MainWindow`` puts it in a fixed toolbar on
the edge named by ``sponsor.position`` and removes that toolbar from the
dock/toolbar context menu.

Any number of logos is supported — they are laid out along the bar, each scaled
to its own or the shared maximum height, and each can carry its own link.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QBoxLayout, QLabel, QWidget

from config.schema import LogoConfig, SponsorAlignment, SponsorConfig

_log = logging.getLogger(__name__)

__all__ = ["SponsorPanel"]


class _LogoLabel(QLabel):
    """One logo. Opens its own link when clicked, if it has one."""

    def __init__(self, logo: LogoConfig, height_px: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._link = logo.link_url
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(height_px)

        pixmap = QPixmap(str(logo.path))
        if pixmap.isNull():
            # The loader already checked the file exists, so this means an
            # unreadable or unsupported image — degrade to text, don't vanish.
            _log.warning("Sponsor logo %s could not be loaded", logo.path)
            self.setText(f"[{logo.path.name}]")
        else:
            self.setPixmap(
                pixmap.scaledToHeight(height_px, Qt.TransformationMode.SmoothTransformation)
            )

        if self._link:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(self._link)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._link and event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl(self._link))
        super().mousePressEvent(event)


class SponsorPanel(QWidget):
    """A strip of sponsor logos, laid out along the bar's axis."""

    def __init__(self, config: SponsorConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        vertical = config.position.is_vertical

        direction = (
            QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight
        )
        layout = QBoxLayout(direction, self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(config.spacing_px)

        # Alignment along the bar: a stretch before and/or after the logos.
        if config.alignment in (SponsorAlignment.CENTER, SponsorAlignment.RIGHT):
            layout.addStretch(1)

        if not config.logos:
            placeholder = QLabel("No sponsor logos configured", self)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(placeholder)
        else:
            for logo in config.logos:
                height = logo.max_height_px or config.max_height_px
                layout.addWidget(_LogoLabel(logo, height, self))

        if config.alignment in (SponsorAlignment.CENTER, SponsorAlignment.LEFT):
            layout.addStretch(1)

        thickness = self._thickness()
        if vertical:
            self.setFixedWidth(thickness)
        else:
            self.setFixedHeight(thickness)

    def _thickness(self) -> int:
        """How deep the bar has to be to fit the tallest logo."""
        heights = [logo.max_height_px or self._config.max_height_px for logo in self._config.logos]
        return max(heights, default=self._config.max_height_px) + 12
