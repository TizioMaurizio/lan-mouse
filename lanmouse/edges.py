"""Screen edge switching: native KWin script on KDE, cursor polling on X11/Windows."""

import os

from PySide6.QtCore import ClassInfo, QObject, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication


@ClassInfo({"D-Bus Interface": "org.lanmouse.Edge"})
class EdgeEndpoint(QObject):
    position = Signal(int, int, int, int, int, int)

    @Slot(int, int, int, int, int, int)
    def moved(self, x, y, left, top, width, height):
        self.position.emit(x, y, left, top, width, height)


class Edges(QObject):
    hit = Signal()
    desktop_changed = Signal(bool)

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.enabled = False
        self.side = "right"
        self.sending = False
        self.position = None
        self.geometry = None
        self.at_edge = True
        self.kwin = None
        self.endpoint = None
        self.available = True
        self.closed = False
        self.description = "Move to the selected screen edge, or press F8."
        self.wayland = os.name != "nt" and bool(os.environ.get("WAYLAND_DISPLAY"))
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(4)
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
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusServiceWatcher

        self.bus = QDBusConnection.sessionBus()
        self.endpoint = EdgeEndpoint(self)
        if not self.bus.registerService("org.lanmouse.Edge"):
            self.available = False
            self.description = "Screen-edge service is busy. Close the other LAN Mouse instance."
            return
        self.bus.registerObject(
            "/Edge", self.endpoint, QDBusConnection.RegisterOption.ExportAllSlots
        )
        self.endpoint.position.connect(self._sample)
        self.watcher = QDBusServiceWatcher(
            "org.kde.KWin",
            self.bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange,
            self,
        )
        self.watcher.serviceOwnerChanged.connect(self._kwin_owner_changed)
        self.kwin = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", self.bus)
        if not self.kwin.isValid():
            self.available = False
            self.description = "KWin scripting is unavailable; press F8 to switch."

    def _kwin_owner_changed(self, service, old_owner, new_owner):
        if self.closed:
            return
        self.position = self.geometry = None
        self.at_edge = True
        self.kwin = None
        self.available = False
        # An owner can be replaced directly, without an intermediate empty name.
        # Restore local input before reconnecting to the new desktop service.
        self.description = "Linux desktop restarted; restoring local control."
        self.desktop_changed.emit(False)
        if new_owner:
            from PySide6.QtDBus import QDBusInterface

            self.kwin = QDBusInterface(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", self.bus
            )
            self.available = self.kwin.isValid()
            if self.available:
                self.description = "Move to the selected screen edge, or press F8."
                self.configure(self.side, self.enabled)
                self.desktop_changed.emit(True)

    def configure(self, side, enabled):
        self.side, self.enabled = side, enabled
        self.at_edge = True
        if self.kwin and self.available:
            self.kwin.call("unloadScript", "lan-mouse-python-edge")
            if enabled:
                path = self.directory / "edge.js"
                path.write_text(
                    "var last = 0;\n"
                    "function report() {\n"
                    "  var p = workspace.cursorPos;\n"
                    "  var g = workspace.virtualScreenGeometry;\n"
                    "  var now = Date.now();\n"
                    "  var edge = p.x <= g.x + 16 || p.x >= g.x + g.width - 17;\n"
                    "  if (!edge && now - last < 4) return;\n"
                    "  last = now;\n"
                    '  callDBus("org.lanmouse.Edge", "/Edge", "org.lanmouse.Edge", "moved",\n'
                    "    Math.round(p.x), Math.round(p.y), g.x, g.y, g.width, g.height);\n"
                    "}\n"
                    "workspace.cursorPosChanged.connect(report);\n"
                    "report();\n"
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
        geometry = screens[0].geometry()
        for screen in screens[1:]:
            geometry = geometry.united(screen.geometry())
        position = QCursor.pos()
        self._sample(
            position.x(),
            position.y(),
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
        )

    def _sample(self, x, y, left, top, width, height):
        self.position = (x, y)
        self.geometry = QRect(left, top, width, height)
        distance = x - left if self.side == "left" else left + width - 1 - x
        # Rearm after moving a few pixels inward, not after a fixed time delay.
        # The compositor can report an edge repeatedly while the pointer is clipped.
        if distance >= 4:
            self.at_edge = False
        elif distance <= 0 and not self.at_edge:
            self.at_edge = True
            self._hit()

    def _hit(self):
        if self.enabled and not self.sending:
            self.hit.emit()

    def crossing(self):
        if self.position is None or self.geometry is None:
            return {"y": 32768}
        g = self.geometry
        y = round((self.position[1] - g.top()) * 65535 / max(1, g.height() - 1))
        return {"y": max(0, min(65535, y))}

    def enter(self, crossing, backend):
        geometry = self.geometry
        if geometry is None:
            screens = QGuiApplication.screens()
            if not screens:
                return
            geometry = screens[0].geometry()
            for screen in screens[1:]:
                geometry = geometry.united(screen.geometry())
        x = geometry.left() + 8 if self.side == "left" else geometry.right() - 8
        y = geometry.top() + round(crossing["y"] * (geometry.height() - 1) / 65535)
        if self.wayland:
            backend.warp(
                round((x - geometry.left()) * 65535 / max(1, geometry.width() - 1)), crossing["y"]
            )
        else:
            QCursor.setPos(x, y)
        self.position = (x, y)
        self.geometry = geometry
        # Ignore any old border reports already queued by the compositor. The
        # first report from the inset position rearms crossing immediately.
        self.at_edge = self.wayland

    def cooldown(self):
        self.at_edge = True

    def close(self):
        self.closed = True
        self.timer.stop()
        if self.kwin:
            self.kwin.call("unloadScript", "lan-mouse-python-edge")
        if self.endpoint:
            self.bus.unregisterObject("/Edge")
            self.bus.unregisterService("org.lanmouse.Edge")
