"""Screen edge switching: native KWin script on KDE, cursor polling on X11/Windows."""

import os
import time

from PySide6.QtCore import ClassInfo, QObject, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication


@ClassInfo({"D-Bus Interface": "org.lanmouse.Edge"})
class EdgeEndpoint(QObject):
    position = Signal(int, int, int, int, int, int)

    @Slot(int, int, int, int, int, int)
    def moved(self, x, y, left, top, width, height):
        self.position.emit(x, y, left, top, width, height)

    @Slot(result="QVariantMap")
    def status(self):
        return self.parent().diagnostics()


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
        self.script_id = None
        self.last_report = 0
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self._load_kwin_script)
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(500)
        self.health_timer.timeout.connect(self._check_kwin_script)
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
        self.reload_timer.stop()
        self.health_timer.stop()
        self.script_id = None
        self.last_report = 0
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
            if not enabled:
                self.reload_timer.stop()
                self.health_timer.stop()
                self.script_id = None
                self.kwin.call("unloadScript", "lan-mouse-python-edge")
            elif self.script_id is None and not self.reload_timer.isActive():
                self._reload_kwin_script()

    def _reload_kwin_script(self):
        self.script_id = None
        self.last_report = time.monotonic()
        self.kwin.call("unloadScript", "lan-mouse-python-edge")
        # KWin deletes scripts asynchronously. Loading immediately can return -1
        # or leave an old script at the D-Bus path we are about to start.
        self.reload_timer.start(100)
        self.health_timer.start()

    def _load_kwin_script(self):
        from PySide6.QtDBus import QDBusMessage

        if self.closed or not self.enabled or not self.kwin or not self.available:
            return
        loaded = self.kwin.call("isScriptLoaded", "lan-mouse-python-edge")
        if loaded.arguments() == [True]:
            self.reload_timer.start(100)
            return
        path = self.directory / "edge.js"
        path.write_text(
            "var last = 0;\n"
            "function report(force) {\n"
            "  var p = workspace.cursorPos;\n"
            "  var g = workspace.virtualScreenGeometry;\n"
            "  var now = Date.now();\n"
            "  var edge = p.x <= g.x + 16 || p.x >= g.x + g.width - 17;\n"
            "  if (!force && !edge && now >= last && now - last < 4) return;\n"
            "  last = now;\n"
            '  callDBus("org.lanmouse.Edge", "/Edge", "org.lanmouse.Edge", "moved",\n'
            "    p.x, p.y, g.x, g.y, g.width, g.height);\n"
            "}\n"
            "workspace.cursorPosChanged.connect(function() { report(false); });\n"
            "workspace.screensChanged.connect(function() { report(true); });\n"
            "var heartbeat = new QTimer();\n"
            "heartbeat.interval = 1000;\n"
            "heartbeat.timeout.connect(function() { report(true); });\n"
            "heartbeat.start();\n"
            "report(true);\n"
        )
        reply = self.kwin.call("loadScript", str(path), "lan-mouse-python-edge")
        arguments = reply.arguments()
        if (
            reply.type() == QDBusMessage.MessageType.ErrorMessage
            or not arguments
            or type(arguments[0]) is not int
            or arguments[0] < 0
        ):
            self.reload_timer.start(500)
            return
        # KWin allocates IDs from the current script count, so a reused ID's
        # D-Bus path can belong to a different plugin. Start through the manager,
        # which addresses the actual loaded objects and skips running scripts.
        result = self.kwin.call("start")
        if result.type() == QDBusMessage.MessageType.ErrorMessage:
            self._reload_kwin_script()
            return
        self.script_id = arguments[0]
        self.last_report = time.monotonic()

    def _check_kwin_script(self):
        if (
            not self.closed
            and self.enabled
            and self.kwin
            and self.available
            and time.monotonic() - self.last_report > 3
        ):
            self._reload_kwin_script()

    def diagnostics(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "sending": self.sending,
            "side": self.side,
            "armed": not self.at_edge,
            "script_id": self.script_id if self.script_id is not None else -1,
            "report_age_ms": round((time.monotonic() - self.last_report) * 1000)
            if self.last_report
            else -1,
            "cursor_x": self.position[0] if self.position else -1,
            "cursor_y": self.position[1] if self.position else -1,
        }

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
        self.last_report = time.monotonic()
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
        self.reload_timer.stop()
        self.health_timer.stop()
        self.timer.stop()
        if self.kwin:
            self.kwin.call("unloadScript", "lan-mouse-python-edge")
        if self.endpoint:
            self.bus.unregisterObject("/Edge")
            self.bus.unregisterService("org.lanmouse.Edge")
