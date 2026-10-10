"""The same GUI on both PCs: choose a computer, approve once, then share."""

import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .clipboard import Clipboard
from .core import PORT, Clock, ProtocolError, decode_clipboard, integer, validate_input
from .desktop import APP_ID, configure_kde_taskbar
from .edges import Edges
from .icons import application_icon
from .identity import Identity
from .network import Network, local_addresses


class Signals(QObject):
    status = Signal(str)
    peers = Signal(list)
    ready = Signal(object)
    disconnected = Signal(object, str)
    toggle = Signal()
    input_error = Signal(str)
    approval = Signal(str, str, object)


class Window(QMainWindow):
    def __init__(self, identity=None, backend_factory=None, start_network=True):
        super().__init__()
        self.identity = identity or Identity()
        self.session = None
        self.owner = None
        self.control = Clock(self.identity.id)
        self.clip_clock = Clock(self.identity.id)
        self.inbox = queue.Queue(maxsize=1024)
        self.last_switch = 0
        self.pending_approvals = []
        self.closing = False
        self.quit_requested = False
        self.hiding_to_tray = False
        self.minimize_timer = QTimer(self)
        self.minimize_timer.setSingleShot(True)
        self.minimize_timer.timeout.connect(self._finish_minimize)
        self.events = Signals()
        self.setWindowTitle("LAN Mouse")
        self.setWindowIcon(application_icon())
        self.resize(580, 710)
        self._build_ui()
        self.events.status.connect(self._status)
        self.events.peers.connect(self._peers)
        self.events.ready.connect(self._connected)
        self.events.disconnected.connect(self._disconnected)
        self.events.toggle.connect(self.toggle)
        self.events.input_error.connect(self._input_error)
        self.events.approval.connect(self._approve_gui)
        if backend_factory is None:
            if sys.platform == "win32":
                from .backends.windows import WindowsInput

                backend_factory = WindowsInput
            elif sys.platform == "linux":
                from .backends.linux import LinuxInput

                backend_factory = LinuxInput
            else:
                raise RuntimeError("This version supports Windows and Linux")
        self.backend = backend_factory(
            self._physical, self.events.toggle.emit, self.events.input_error.emit
        )
        self.clipboard = Clipboard(self)
        self.clipboard.changed.connect(self._clipboard_changed)
        self.clipboard.warning.connect(self._status)
        self.edges = Edges(self.identity.directory, self)
        self.edges.hit.connect(self._edge_hit)
        self.edges.desktop_changed.connect(self._desktop_changed)
        self.edge_hint.setText(self.edges.description)
        self.network = Network(
            self.identity,
            self._approve,
            self.events.ready.emit,
            self._receive,
            self._closed_thread,
            self.events.peers.emit,
            self.events.status.emit,
        )
        self._configure_edges()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(2)
        self.timer.timeout.connect(self._drain)
        self.timer.start()
        self._tray()
        if start_network:
            try:
                self.network.start()
            except OSError as exc:
                self._status(f"Could not start network: {exc}. Close any other LAN Mouse instance.")

    def _build_ui(self):
        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(12)
        title = QLabel("LAN Mouse")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("One mouse. One keyboard. Both computers.")
        layout.addWidget(subtitle)
        addresses = ", ".join(local_addresses()) or "No LAN address found"
        self.local = QLabel(f"This computer: {self.identity.name}\nIP address: {addresses}")
        self.local.setTextFormat(Qt.TextFormat.PlainText)
        self.local.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.local)
        layout.addWidget(QLabel("Nearby computers"))
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        self.list.itemDoubleClicked.connect(lambda _: self.connect_selected())
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_selected)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(lambda: self.network.disconnect())
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("Other computer's IP, if it does not appear above")
        self.address.returnPressed.connect(self.connect_ip)
        self.ip_button = QPushButton("Connect by IP")
        self.ip_button.clicked.connect(self.connect_ip)
        row.addWidget(self.address)
        row.addWidget(self.ip_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("The other computer is on my"))
        self.side = QComboBox()
        self.side.addItems(["Right", "Left"])
        default = "left" if os.name == "nt" else "right"
        self.side.setCurrentIndex(1 if self.identity.settings.get("side", default) == "left" else 0)
        self.side.currentIndexChanged.connect(self._configure_edges)
        row.addWidget(self.side)
        row.addStretch()
        layout.addLayout(row)
        self.edge_check = QCheckBox("Switch at the screen edge")
        self.edge_check.setChecked(self.identity.settings.get("edges", True))
        self.edge_check.toggled.connect(self._configure_edges)
        layout.addWidget(self.edge_check)
        self.edge_hint = QLabel()
        self.edge_hint.setWordWrap(True)
        layout.addWidget(self.edge_hint)
        self.clip_check = QCheckBox("Synchronize clipboard (text and images)")
        self.clip_check.setChecked(self.identity.settings.get("clipboard", True))
        self.clip_check.toggled.connect(self._clipboard_option)
        layout.addWidget(self.clip_check)
        self.switch_button = QPushButton("Control the other computer · F8")
        self.switch_button.setEnabled(False)
        self.switch_button.clicked.connect(self.toggle)
        layout.addWidget(self.switch_button)
        self.state_label = QLabel("Waiting for the other computer")
        self.state_label.setObjectName("state")
        self.state_label.setWordWrap(True)
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.state_label)
        self.detail = QLabel("Open LAN Mouse on both PCs, choose a computer, and approve once.")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        self.setStyleSheet("""
            QMainWindow { background: #151c28; }
            QWidget { color: #e8eef9; font-size: 13px; }
            QLabel#title { font-size: 30px; font-weight: 700; }
            QLabel#state { color: #79dbb3; font-weight: 600; }
            QListWidget { background: #202b3e; border: 1px solid #41516b;
                border-radius: 6px; }
            QLineEdit, QComboBox { background: #202b3e;
                border: 1px solid #41516b; border-radius: 6px; padding: 8px; }
            QPushButton { background: #315eac; border: 0; border-radius: 6px;
                padding: 10px 14px; }
            QPushButton:hover { background: #3d72c8; }
            QPushButton:disabled { background: #28344a; color: #8a99af; }
            QCheckBox { spacing: 8px; }
        """)

    def _tray(self):
        self.tray = None
        has_tray = QSystemTrayIcon.isSystemTrayAvailable()
        configure_kde_taskbar(has_tray)
        if not has_tray:
            return
        # A utility window stays out of the normal taskbar/Alt-Tab list. The
        # tray icon remains the way to reopen settings while sharing continues.
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.tray = QSystemTrayIcon(application_icon(), self)
        menu = QMenu(self)
        show = QAction("Show LAN Mouse", self)
        show.triggered.connect(self.show_window)
        switch = QAction("Switch computer (F8)", self)
        switch.triggered.connect(self.toggle)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(switch)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("LAN Mouse · F8 switches computers")
        self.tray.activated.connect(
            lambda reason: (
                self.show_window()
                if reason
                in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                )
                else None
            )
        )
        self.tray.show()

    def tray_available(self):
        return bool(self.tray and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable())

    def hide_to_tray(self):
        self.minimize_timer.stop()
        self.hiding_to_tray = True
        try:
            self.hide()
            # Clear the minimized state while hidden, so the next tray click
            # restores a normal settings window instead of a taskbar item.
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        finally:
            self.hiding_to_tray = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and not self.hiding_to_tray
            and self.tray_available()
        ):
            # showMinimized() can still call show() after this event returns.
            self.minimize_timer.start(0)

    def _finish_minimize(self):
        if not self.closing and self.isMinimized() and self.tray_available():
            self.hide_to_tray()

    def present_at_startup(self):
        if not self.tray_available():
            self.show_window()

    def show_window(self):
        self.minimize_timer.stop()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self):
        self.quit_requested = True
        self.close()
        QApplication.instance().quit()

    def _status(self, text):
        self.detail.setText(text)

    def _peers(self, peers):
        selected = self.list.currentItem()
        previous = selected.data(Qt.ItemDataRole.UserRole)["id"] if selected else None
        self.list.clear()
        for peer in peers:
            item = QListWidgetItem(f"{peer['name']}   ·   {peer['address']}")
            item.setData(Qt.ItemDataRole.UserRole, peer)
            self.list.addItem(item)
            if peer["id"] == previous or self.list.count() == 1:
                self.list.setCurrentItem(item)

    def connect_selected(self):
        selected = self.list.currentItem()
        if not selected:
            self._status("Choose a nearby computer, or enter its IP address below.")
            return
        peer = selected.data(Qt.ItemDataRole.UserRole)
        self.network.connect(peer["address"], peer["port"], peer["id"])

    def connect_ip(self):
        self.network.connect(self.address.text().strip(), PORT)

    def _approve(self, name, address):
        request = {"done": threading.Event(), "accepted": False}
        self.events.approval.emit(name, address, request)
        request["done"].wait(80)
        return request["accepted"] and not self.closing

    def _approve_gui(self, name, address, request):
        if self.closing:
            request["done"].set()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Connect this computer?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"Allow {name} ({address}) to share mouse, keyboard and clipboard?")
        box.setInformativeText(
            "Approve only the computer you are connecting now. "
            "The app will remember it for future connections."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        self.pending_approvals.append(request)
        request["accepted"] = box.exec() == QMessageBox.StandardButton.Yes
        request["done"].set()
        self.pending_approvals.remove(request)

    def _connected(self, session):
        if self.closing or session.closed.is_set():
            session.close()
            return
        self.session = session
        self.owner = None
        self.control = Clock(self.identity.id)
        self.clip_clock = Clock(self.identity.id)
        self.backend.enabled = True
        self.switch_button.setEnabled(self.backend.ready)
        self.connect_button.setEnabled(False)
        self.ip_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.state_label.setText(f"Connected to {session.name} · Local control")
        self._status("Move to the selected edge, or press F8. Press F8 again to return.")
        self._configure_edges()
        session.start()
        self.clipboard.enable(self.clip_check.isChecked(), publish_initial=session.initiator)

    def _closed_thread(self, session, reason):
        # Restore physical input immediately; do not wait for the GUI's event queue.
        if self.session is session:
            self.backend.set_active(False)
            self.backend.receiving = False
            self.backend.release_all()
        self.events.disconnected.emit(session, reason)

    def _disconnected(self, session, reason):
        if self.session is not session:
            return
        self.session = None
        self.owner = None
        self.backend.enabled = False
        self.clipboard.enable(False)
        self._configure_edges()
        self.switch_button.setEnabled(False)
        self.connect_button.setEnabled(True)
        self.ip_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.state_label.setText("Disconnected · Local control")
        self._status(reason)

    def _physical(self, message):
        session = self.session
        if session and self.owner == self.identity.id:
            session.send({**message, "stamp": list(self.control.current)})

    def _receive(self, session, message):
        try:
            self.inbox.put_nowait((session, message))
        except queue.Full:
            session.close("Input queue full; local control restored")

    def _drain(self):
        self.backend.lease = time.monotonic()
        for _ in range(128):
            try:
                session, message = self.inbox.get_nowait()
            except queue.Empty:
                break
            if session is not self.session or session.closed.is_set():
                continue
            try:
                self._message(message)
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                session.close(f"Sharing stopped: {exc}")

    def _message(self, message):
        kind = message["type"]
        if kind == "control":
            owner = message.get("owner")
            if owner not in (None, self.session.peer_id, self.identity.id):
                raise ProtocolError("Invalid control owner")
            crossing = message.get("edge")
            if crossing is not None:
                if not isinstance(crossing, dict):
                    raise ProtocolError("Invalid screen crossing")
                integer(crossing.get("y"), 0, 65535)
            if self.control.accept(message.get("stamp"), self.session.peer_id):
                self._set_owner(owner, crossing)
        elif kind == "input":
            event = validate_input(message)
            if self.owner == self.session.peer_id and message.get("stamp") == list(
                self.control.current
            ):
                self.backend.inject(event)
        elif kind == "clipboard":
            if self.clip_check.isChecked():
                mime, raw = decode_clipboard(message)
                if self.clip_clock.accept(message.get("stamp"), self.session.peer_id):
                    self.clipboard.apply(mime, raw)
        else:
            raise ProtocolError("Unknown message type")

    def toggle(self, crossing=None):
        if not isinstance(crossing, dict):
            crossing = None  # QPushButton.clicked supplies a bool.
        if not self.session or self.session.closed.is_set():
            return
        if (
            crossing is None
            and time.monotonic() - self.last_switch < 0.25
            and self.owner != self.identity.id
        ):
            return
        self.last_switch = time.monotonic()
        # Either side can return to local control. From local mode, use this PC's devices.
        owner = self.identity.id if self.owner is None else None
        if owner and not self.backend.ready:
            self._status("Input is unavailable. Complete Linux setup and restart the app.")
            return
        control = {"type": "control", "owner": owner, "stamp": self.control.tick()}
        if crossing is not None:
            control["edge"] = crossing
        self.session.send(control)
        try:
            self._set_owner(owner, crossing)
        except (OSError, RuntimeError) as exc:
            self.session.send({"type": "control", "owner": None, "stamp": self.control.tick()})
            self._set_owner(None)
            self._status(str(exc))

    def _set_owner(self, owner, crossing=None):
        self.backend.set_active(False)
        self.backend.release_all()
        self.backend.receiving = owner is not None and owner != self.identity.id
        self.owner = owner
        if owner == self.identity.id:
            self.backend.set_active(True)
            self.state_label.setText(f"Controlling {self.session.name} · F8 returns here")
            self.switch_button.setText("Return to this computer · F8")
        elif owner:
            self.state_label.setText(f"Controlled by {self.session.name} · F8 returns to local")
            self.switch_button.setText("Return to local control · F8")
        else:
            self.state_label.setText(f"Connected to {self.session.name} · Local control")
            self.switch_button.setText("Control the other computer · F8")
        self.edges.cooldown()
        self.edges.sending = owner == self.identity.id
        if crossing is not None and owner != self.identity.id:
            self.edges.enter(crossing, self.backend)

    def _input_error(self, text):
        if self.session and self.owner is not None:
            self.session.send({"type": "control", "owner": None, "stamp": self.control.tick()})
            self._set_owner(None)
        self.switch_button.setEnabled(bool(self.session and self.backend.ready))
        self._status(text)

    def _edge_hit(self):
        if self.owner != self.identity.id:
            self.toggle(self.edges.crossing())

    def _desktop_changed(self, available):
        if self.closing:
            return
        if not available:
            self.backend.set_active(False)
            self.backend.receiving = False
            self.backend.release_all()
            if self.session:
                session = self.session
                reason = "Linux desktop restarted; reconnecting automatically."
                session.close(reason)
                self._disconnected(session, reason)
        else:
            self._status("Linux desktop recovered; waiting for the paired computer.")
        self.edge_hint.setText(self.edges.description)

    def _configure_edges(self, *_):
        if not hasattr(self, "edges"):
            return
        self.edges.sending = self.owner == self.identity.id
        side = self.side.currentText().lower()
        self.identity.settings.update(side=side, edges=self.edge_check.isChecked())
        self.identity.save()
        self.edges.configure(side, bool(self.session) and self.edge_check.isChecked())
        self.edge_hint.setText(self.edges.description)

    def _clipboard_changed(self, mime, raw):
        if self.session and self.clip_check.isChecked():
            self.session.send(
                {
                    "type": "clipboard",
                    "mime": mime,
                    "data": raw,
                    "stamp": self.clip_clock.tick(),
                }
            )

    def _clipboard_option(self, checked):
        self.identity.settings["clipboard"] = checked
        self.identity.save()
        if hasattr(self, "clipboard"):
            self.clipboard.enable(bool(self.session) and checked)

    def closeEvent(self, event):
        if not self.quit_requested:
            event.ignore()
            if self.tray_available():
                self.hide_to_tray()
            else:
                # Keep an accessible taskbar window when a desktop has no tray.
                self.showMinimized()
            return
        if self.closing:
            event.accept()
            return
        self.closing = True
        self.minimize_timer.stop()
        for request in self.pending_approvals:
            request["done"].set()
        self.timer.stop()
        self.backend.close()
        self.clipboard.close()
        self.edges.close()
        if self.tray:
            self.tray.hide()
        # Physical input is already released; discovery shutdown can take a little longer.
        self.network.stop()
        event.accept()


def main():
    if os.name == "nt":
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    application = QApplication(sys.argv)
    application.setApplicationName("LAN Mouse")
    application.setDesktopFileName(APP_ID)
    application.setWindowIcon(application_icon())
    application.setQuitOnLastWindowClosed(False)
    try:
        window = Window()
    except Exception as exc:
        QMessageBox.critical(None, "LAN Mouse", f"Could not start LAN Mouse:\n{exc}")
        raise SystemExit(1) from exc
    window.present_at_startup()
    raise SystemExit(application.exec())
