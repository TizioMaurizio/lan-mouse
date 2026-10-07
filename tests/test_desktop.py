import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from lanmouse.clipboard import TEXT, Clipboard
from lanmouse.core import Clock
from lanmouse.identity import Identity


@pytest.fixture(scope="module")
def qt():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def clipboard(qt, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    value = Clipboard()
    value.enable(True)
    yield value
    value.close()


def test_clipboard_receive_does_not_echo(clipboard):
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    clipboard.apply(TEXT, "hello è 🙂".encode())
    assert clipboard.clipboard.text() == "hello è 🙂"
    assert changes == []
    clipboard.clipboard.setText("new local copy")
    assert changes == [(TEXT, b"new local copy")]


def test_image_receive_does_not_echo(clipboard):
    image = QImage(12, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#31aa88"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    clipboard.apply("image/png", bytes(data))
    assert clipboard.clipboard.image().size() == image.size()
    assert changes == []


def test_clipboard_disabled_does_not_read_or_write(clipboard):
    clipboard.clipboard.setText("keep this")
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    clipboard.enable(False)
    clipboard.apply(TEXT, b"incoming")
    assert clipboard.clipboard.text() == "keep this"
    assert not changes


class FakeEdges(QObject):
    hit = Signal()
    description = "Move to the selected screen edge, or press F8."

    def __init__(self, *args):
        super().__init__()

    def configure(self, *args):
        pass

    def cooldown(self):
        pass

    def close(self):
        pass


class FakeBackend:
    ready = True
    active = False
    receiving = False
    enabled = False

    def __init__(self, *args):
        self.injected = []
        self.releases = 0

    def set_active(self, active):
        self.active = active

    def release_all(self):
        self.releases += 1

    def inject(self, message):
        self.injected.append(message)

    def close(self):
        self.set_active(False)
        self.release_all()


class FakeSession:
    peer_id = "other"
    name = "Windows PC"
    initiator = False

    def __init__(self):
        import threading

        self.closed = threading.Event()
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True

    def start(self):
        pass

    def close(self, *args):
        self.closed.set()


@pytest.fixture
def window(qt, tmp_path, monkeypatch):
    import lanmouse.app as app

    monkeypatch.setattr(app, "Edges", FakeEdges)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    value = app.Window(Identity(tmp_path / "gui", "Linux PC"), FakeBackend, start_network=False)
    value._connected(FakeSession())
    yield value
    value.close()


def test_switch_forward_and_return(window):
    window.toggle()
    assert window.backend.active
    assert window.owner == window.identity.id
    window._physical({"type": "input", "kind": "key", "code": 30, "down": True})
    assert window.session.sent[-1]["stamp"] == list(window.control.current)
    # Immediate emergency return is allowed even within the debounce interval.
    window.toggle()
    assert not window.backend.active
    assert window.owner is None
    assert window.session.sent[-1]["owner"] is None


def test_receiver_ignores_input_from_old_control_session(window):
    window._message({"type": "control", "owner": "other", "stamp": [1, "other"]})
    assert window.backend.receiving
    event = {"type": "input", "kind": "key", "code": 30, "down": True, "stamp": [1, "other"]}
    window._message(event)
    assert len(window.backend.injected) == 1
    window._message({"type": "control", "owner": None, "stamp": [2, "other"]})
    window._message(event)
    assert len(window.backend.injected) == 1
    assert window.backend.releases > 0


def test_connection_loss_restores_input_without_waiting_for_gui(window):
    window.toggle()
    assert window.backend.active
    window._closed_thread(window.session, "connection lost")
    assert not window.backend.active
    assert not window.backend.receiving


def test_simultaneous_switches_converge(window):
    window.toggle()
    remote = Clock("other")
    stamp = remote.tick()
    window._message({"type": "control", "owner": "other", "stamp": stamp})
    remote.accept(list(window.session.sent[0]["stamp"]), window.identity.id)
    assert window.control.current == remote.current


def test_qt_gui_renders(window, qt):
    window.show()
    qt.processEvents()
    assert not window.grab().isNull()
    assert window.connect_button.text() == "Connect"
    assert "Local control" in window.state_label.text()
    assert window.connect_button.geometry().top() > window.list.geometry().bottom()


def test_wayland_clipboard_sync_without_feedback(qt, monkeypatch):
    import time

    import lanmouse.clipboard as module

    monkeypatch.setenv("WAYLAND_DISPLAY", "isolated-test")
    content = [(TEXT, b"initial")]
    monkeypatch.setattr(Clipboard, "_wayland_read", lambda self: content[0])
    monkeypatch.setattr(module.shutil, "which", lambda command: command)

    def run(command, **kwargs):
        assert command[0] == "wl-copy"
        content[0] = (command[-1], kwargs["input"])

    monkeypatch.setattr(module.subprocess, "run", run)
    value = Clipboard()
    changes = []
    value.changed.connect(lambda *args: changes.append(args))

    def wait_until(predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("Clipboard worker timed out")

    try:
        value.enable(True)
        wait_until(lambda: value.last is not None)
        assert not changes  # Receiver's initial clipboard is not sent.
        value.apply(TEXT, b"remote copy")
        wait_until(lambda: content[0][1] == b"remote copy")
        assert not changes  # Applying the received content must not echo it back.
        content[0] = (TEXT, b"new local copy")
        wait_until(lambda: bool(changes))
        assert changes == [(TEXT, b"new local copy")]
    finally:
        value.close()
