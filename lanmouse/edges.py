"""Screen edge switching: native KWin script on KDE, cursor polling on X11/Windows."""

import os
import time

from PySide6.QtCore import ClassInfo, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication


@ClassInfo({"D-Bus Interface": "org.lanmouse.Edge"})
class EdgeEndpoint(QObject):
    hit = Signal()

    @Slot()
    def activate(self):
        self.hit.emit()


class Edges(QObject):
    hit = Signal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.enabled = False
        self.side = "right"
        self.last_hit = 0
        self.at_edge = True
        self.kwin = None
        self.endpoint = None
        self.available = True
        self.description = "Move to the selected screen edge, or press F8."
        self.wayland = os.name != "nt" and bool(os.environ.get("WAYLAND_DISPLAY"))
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._poll)
        if self.wayland:
            if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", ""):
                self._kwin_init()
            else:
                self.available = False
                self.description = "On this Wayland desktop, press F8 to switch computers."
        else:
            self.timer.start()

    def _kwin_init(self):
        from PySide6.QtDBus import QDBusConnection, QDBusInterface

        self.bus = QDBusConnection.sessionBus()
        self.endpoint = EdgeEndpoint(self)
        if not self.bus.registerService("org.lanmouse.Edge"):
            self.available = False
            self.description = "Screen-edge service is busy. Close the other LAN Mouse instance."
            return
        self.bus.registerObject(
            "/Edge", self.endpoint, QDBusConnection.RegisterOption.ExportAllSlots
        )
        self.endpoint.hit.connect(self._hit)
        self.kwin = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", self.bus)
        if not self.kwin.isValid():
            self.available = False
            self.description = "KWin scripting is unavailable; press F8 to switch."

    def configure(self, side, enabled):
        self.side, self.enabled = side, enabled
        self.at_edge = True
        if self.kwin and self.available:
            self.kwin.call("unloadScript", "lan-mouse-python-edge")
            if enabled:
                path = self.directory / "edge.js"
                border = "KWin.ElectricRight" if side == "right" else "KWin.ElectricLeft"
                path.write_text(
                    f"registerScreenEdge({border}, function() {{\n"
                    '  callDBus("org.lanmouse.Edge", "/Edge", "org.lanmouse.Edge", "activate");\n'
                    "});\n"
                )
                reply = self.kwin.call("loadScript", str(path), "lan-mouse-python-edge")
                from PySide6.QtDBus import QDBusInterface, QDBusMessage

                if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                    self.available = False
                    self.description = "KWin could not load the edge script; press F8 to switch."
                    return
                script_id = reply.arguments()[0]
                script = QDBusInterface(
                    "org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script", self.bus
                )
                # Plasma 6 exports scripts under /Scripting/ScriptN.
                if not script.isValid():
                    script = QDBusInterface(
                        "org.kde.KWin", f"/{script_id}", "org.kde.kwin.Script", self.bus
                    )
                result = script.call("run")
                if result.type() == QDBusMessage.MessageType.ErrorMessage:
                    self.available = False
                    self.description = "KWin could not start edge switching; press F8 to switch."

    def _poll(self):
        if not self.enabled:
            return
        screens = QGuiApplication.screens()
        if not screens:
            return
        position = QCursor.pos()
        # Only the outside edge of the whole desktop, so internal monitor edges stay usable.
        left = min(screen.geometry().left() for screen in screens)
        right = max(screen.geometry().right() for screen in screens)
        at_edge = position.x() <= left if self.side == "left" else position.x() >= right
        if at_edge and not self.at_edge:
            self._hit()
        self.at_edge = at_edge

    def _hit(self):
        if self.enabled and time.monotonic() - self.last_hit > 1.3:
            self.last_hit = time.monotonic()
            self.hit.emit()

    def cooldown(self):
        self.last_hit = time.monotonic()
        self.at_edge = True

    def close(self):
        self.timer.stop()
        if self.kwin:
            self.kwin.call("unloadScript", "lan-mouse-python-edge")
        if self.endpoint:
            self.bus.unregisterObject("/Edge")
            self.bus.unregisterService("org.lanmouse.Edge")
