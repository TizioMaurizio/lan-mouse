import os

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage

from lanmouse.clipboard import TEXT, Clipboard
from lanmouse.core import Clock
from lanmouse.identity import Identity


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
    desktop_changed = Signal(bool)
    description = "Move to the selected screen edge, or press F8."

    def __init__(self, *args):
        super().__init__()

    def configure(self, *args):
        pass

    def cooldown(self):
        pass

    def crossing(self):
        return {"y": 12345}

    def enter(self, crossing, backend):
        self.entered = crossing

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
    value.quit()


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


@pytest.mark.skipif(os.name == "nt", reason="Wayland clipboard adapter is Linux-only")
def test_wayland_clipboard_sync_without_feedback(qt, monkeypatch):
    import time

    import lanmouse.clipboard as module

    monkeypatch.setenv("WAYLAND_DISPLAY", "isolated-test")
    monkeypatch.setattr(Clipboard, "_watch_wayland", lambda self: None)
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


def test_copy_image_with_browser_url_is_shared(clipboard, qt):
    import time

    content = QMimeData()
    image = QImage(30, 20, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    content.setImageData(image)
    content.setUrls([QUrl("https://example.com/image.jpg")])
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    clipboard.clipboard.setMimeData(content)
    end = time.monotonic() + 3
    while not changes and time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.005)
    assert len(changes) == 1
    mime, raw = changes[0]
    assert mime == "image/png"
    assert QImage.fromData(raw).size() == image.size()


def test_image_receive_advertises_native_image_and_png(clipboard):
    image = QImage(5, 5, QImage.Format.Format_ARGB32)
    image.fill(QColor("blue"))
    raw = clipboard._png(image)
    clipboard.apply("image/png", raw)
    mime = clipboard.clipboard.mimeData()
    assert mime.hasImage()
    assert bytes(mime.data("image/png")) == raw


@pytest.mark.parametrize("format", ["PNG", "JPEG", "BMP", "WEBP"])
def test_wayland_image_offer_takes_precedence_over_url(qt, monkeypatch, format):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    from types import SimpleNamespace

    import lanmouse.clipboard as module

    image = QImage(20, 10, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, format):
        pytest.skip(f"Qt lacks {format} writer")
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "BMP": "image/bmp", "WEBP": "image/webp"}[
        format
    ]
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=f"text/uri-list\n{mime}\n".encode()),
    )
    monkeypatch.setattr(Clipboard, "_paste", staticmethod(lambda requested: bytes(data)))
    value = Clipboard()
    try:
        offered, raw = value._wayland_read()
        assert offered == "image/png"
        assert QImage.fromData(raw).size() == image.size()
    finally:
        value.close()


def test_image_compression_does_not_block_gui_or_overwrite_new_copy(clipboard, qt, monkeypatch):
    import threading
    import time

    started, finish = threading.Event(), threading.Event()
    original = Clipboard._png

    def encode(image):
        started.set()
        assert finish.wait(3)
        return original(image)

    monkeypatch.setattr(Clipboard, "_png", staticmethod(encode))
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    image = QImage(20, 10, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        clipboard.clipboard.setImage(image)
        assert started.wait(3)  # setImage returned while compression is still running.
        clipboard.clipboard.setText("newer text")
        finish.set()
        end = time.monotonic() + 0.15
        while time.monotonic() < end:
            qt.processEvents()
            time.sleep(0.005)
        assert changes == [(TEXT, b"newer text")]
    finally:
        finish.set()


def test_remote_edge_handoff_positions_pointer(window):
    crossing = {"y": 23456}
    window._message({"type": "control", "owner": "other", "stamp": [1, "other"], "edge": crossing})
    assert window.backend.receiving
    assert window.edges.entered == crossing
    assert not window.edges.sending
    window._edge_hit()
    assert window.owner is None
    assert window.session.sent[-1]["edge"] == {"y": 12345}


def test_quick_reverse_edge_crossing_is_not_blocked_by_hotkey_debounce(window):
    import time

    window.last_switch = time.monotonic()
    window._edge_hit()
    assert window.backend.active
    assert window.edges.sending
    window._message({"type": "control", "owner": None, "stamp": [2, "other"], "edge": {"y": 12345}})
    assert not window.backend.active
    assert window.edges.entered == {"y": 12345}


def test_invalid_edge_position_rejected(window):
    with pytest.raises(ValueError):
        window._message(
            {"type": "control", "owner": "other", "stamp": [1, "other"], "edge": {"y": 65536}}
        )


def test_duplicate_image_notifications_do_not_cancel_pending_compression(
    clipboard, qt, monkeypatch
):
    import threading
    import time

    started, finish = threading.Event(), threading.Event()
    original = Clipboard._png

    def encode(image):
        started.set()
        assert finish.wait(3)
        return original(image)

    monkeypatch.setattr(Clipboard, "_png", staticmethod(encode))
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    image = QImage(25, 15, QImage.Format.Format_ARGB32)
    image.fill(QColor("yellow"))
    try:
        clipboard.clipboard.setImage(image)
        assert started.wait(3)
        clipboard._qt_changed()  # Some clipboard owners announce the same offer twice.
        finish.set()
        end = time.monotonic() + 3
        while not changes and time.monotonic() < end:
            qt.processEvents()
            time.sleep(0.005)
        assert len(changes) == 1 and changes[0][0] == "image/png"
    finally:
        finish.set()


def test_explicit_png_with_url_is_shared_without_native_image(clipboard, qt):
    import time

    image = QImage(10, 5, QImage.Format.Format_ARGB32)
    image.fill(QColor("cyan"))
    content = QMimeData()
    content.setData("image/png", QByteArray(clipboard._png(image)))
    content.setUrls([QUrl("https://example.com/image.png")])
    changes = []
    clipboard.changed.connect(lambda *args: changes.append(args))
    clipboard.clipboard.setMimeData(content)
    end = time.monotonic() + 3
    while not changes and time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.005)
    assert len(changes) == 1
    assert QImage.fromData(changes[0][1]).size() == image.size()


def test_desktop_loss_releases_input_and_keeps_automatic_reconnect(window):
    window.toggle()
    session = window.session
    assert window.backend.active
    window.edges.desktop_changed.emit(False)
    assert session.closed.is_set()
    assert window.session is None
    assert window.owner is None
    assert not window.backend.active
    assert not window.backend.receiving
    assert not window.edges.sending
    assert not window.network.paused
    window._connected(FakeSession())
    window._edge_hit()
    assert window.backend.active  # A fresh connection must rearm screen switching.


def test_network_disconnect_rearms_edges_after_reconnect(window):
    window.toggle()
    session = window.session
    window._closed_thread(session, "lost network")
    window._disconnected(session, "lost network")
    assert not window.edges.sending
    window._connected(FakeSession())
    window._edge_hit()
    assert window.backend.active


def test_window_close_hides_to_tray_without_stopping_sharing(window, qt, monkeypatch):
    from types import SimpleNamespace

    import lanmouse.app as module

    window.tray = SimpleNamespace(isVisible=lambda: True, hide=lambda: None)
    monkeypatch.setattr(module.QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    window.show()
    window.toggle()
    session = window.session
    window.close()
    qt.processEvents()
    assert not window.isVisible()
    assert not window.closing
    assert not session.closed.is_set()
    assert window.backend.active
    assert window.timer.isActive()
    window.show_window()
    assert window.isVisible() and not window.isMinimized()


def test_window_close_minimizes_when_tray_is_unavailable(window, qt, monkeypatch):
    import lanmouse.app as module

    monkeypatch.setattr(module.QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    window.show()
    window.close()
    qt.processEvents()
    assert window.isVisible() and window.isMinimized()
    assert not window.closing
    assert window.timer.isActive()


def test_explicit_quit_stops_sharing_and_releases_input(window):
    window.toggle()
    window.quit()
    assert window.closing
    assert not window.backend.active
    assert not window.timer.isActive()
    assert window.clipboard.closed.is_set()
    assert window.network.stopped.is_set()
