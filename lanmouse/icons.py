"""Shared scalable icon for the settings window, tray, and desktop launcher."""

from pathlib import Path

from PySide6.QtGui import QIcon

ICON_PATH = Path(__file__).with_name("assets") / "lan-mouse.svg"


def application_icon():
    return QIcon(str(ICON_PATH))
