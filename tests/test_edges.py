import sys

import pytest
from PySide6.QtCore import QRect

from lanmouse.edges import Edges


@pytest.fixture
def edges(qt, tmp_path, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    value = Edges(tmp_path)
    value.configure("right", True)
    yield value
    value.close()


def test_edge_crosses_once_and_rearms_on_retreat_without_time_delay(edges):
    hits = []
    edges.hit.connect(lambda: hits.append(True))
    edges._sample(900, 400, 0, 0, 1000, 800)
    edges._sample(999, 400, 0, 0, 1000, 800)
    edges._sample(999, 401, 0, 0, 1000, 800)
    assert hits == [True]
    edges._sample(993, 401, 0, 0, 1000, 800)
    edges._sample(999, 401, 0, 0, 1000, 800)
    assert hits == [True, True]


def test_outgoing_pointer_cannot_trigger_a_return(edges):
    hits = []
    edges.hit.connect(lambda: hits.append(True))
    edges.sending = True
    edges._sample(100, 0, 0, 0, 1000, 800)
    edges._sample(999, 0, 0, 0, 1000, 800)
    assert not hits


def test_wayland_entry_preserves_height_and_insets_from_joining_edge(edges):
    class Pointer:
        def warp(self, x, y):
            self.position = (x, y)

    pointer = Pointer()
    edges.wayland = True
    edges.side = "left"
    edges.geometry = QRect(-1000, 0, 1000, 800)
    edges.enter({"y": 16384}, pointer)
    assert pointer.position == (round(8 * 65535 / 999), 16384)
    assert edges.position == (-992, 200)
    assert edges.at_edge
    edges._sample(-1000, 200, -1000, 0, 1000, 800)  # Old, queued border report.
    assert edges.at_edge
    edges._sample(-992, 200, -1000, 0, 1000, 800)
    assert not edges.at_edge
    assert edges.crossing()["y"] == pytest.approx(16384, abs=50)


def test_windows_entry_rearms_before_the_next_poll(edges):
    hits = []
    edges.hit.connect(lambda: hits.append(True))
    edges.geometry = QRect(0, 0, 1000, 800)
    edges.enter({"y": 32768}, None)
    assert not edges.at_edge
    edges._sample(999, 400, 0, 0, 1000, 800)
    assert hits == [True]


@pytest.mark.skipif(sys.platform != "linux", reason="KWin D-Bus adapter is Linux-only")
def test_kwin_service_replacement_releases_input_and_reloads_script(edges, monkeypatch):
    from types import SimpleNamespace

    from PySide6 import QtDBus

    notices, configurations = [], []
    edges.desktop_changed.connect(notices.append)
    edges.position = (999, 100)
    edges.geometry = QRect(0, 0, 1000, 800)
    edges.bus = object()
    monkeypatch.setattr(
        QtDBus, "QDBusInterface", lambda *args: SimpleNamespace(isValid=lambda: True)
    )
    monkeypatch.setattr(edges, "configure", lambda *args: configurations.append(args))
    edges._kwin_owner_changed("org.kde.KWin", "old-owner", "new-owner")
    assert notices == [False, True]
    assert edges.position is None and edges.geometry is None
    assert edges.available
    assert configurations == [("right", True)]
    edges.kwin = None  # This test's interface deliberately implements only isValid.


def test_kwin_loss_does_not_call_a_dead_interface(edges):
    notices = []
    edges.desktop_changed.connect(notices.append)
    edges._kwin_owner_changed("org.kde.KWin", "old-owner", "")
    assert notices == [False]
    assert not edges.available
    assert edges.kwin is None


class Reply:
    def __init__(self, *arguments):
        self.values = list(arguments)

    def arguments(self):
        return self.values

    def type(self):
        from PySide6.QtDBus import QDBusMessage

        return QDBusMessage.MessageType.ReplyMessage


class KWinStub:
    def __init__(self):
        self.calls = []
        self.pending_deletion = 0

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "isScriptLoaded":
            if self.pending_deletion:
                self.pending_deletion -= 1
                return Reply(True)
            return Reply(False)
        if method == "loadScript":
            return Reply(7)
        return Reply()


@pytest.mark.skipif(sys.platform != "linux", reason="KWin D-Bus adapter is Linux-only")
def test_reload_waits_for_deferred_script_deletion(edges):
    kwin = KWinStub()
    kwin.pending_deletion = 2
    edges.kwin = kwin
    edges.bus = object()
    edges.configure("right", True)
    assert not any(call[0] == "loadScript" for call in kwin.calls)
    edges._load_kwin_script()
    edges._load_kwin_script()
    assert not any(call[0] == "loadScript" for call in kwin.calls)
    edges._load_kwin_script()
    assert edges.script_id == 7
    assert kwin.calls[-1] == ("start",)


def test_live_cursor_reports_prevent_unnecessary_reload(edges):
    edges.kwin = KWinStub()
    edges.script_id = 7
    edges._sample(500, 200, 0, 0, 1000, 800)
    edges._check_kwin_script()
    edges.configure("left", True)
    assert edges.kwin.calls == []
    assert edges.script_id == 7


def test_stalled_loaded_script_is_restarted_without_disconnect(edges):
    import time

    edges.kwin = KWinStub()
    edges.script_id = 7
    edges.last_report = time.monotonic() - 4
    edges._check_kwin_script()
    assert edges.script_id is None
    assert edges.reload_timer.isActive()
    assert edges.kwin.calls == [("unloadScript", "lan-mouse-python-edge")]
    assert edges.enabled  # Recovery must not disable sharing or require F8.


def test_disabling_or_closing_cancels_pending_script_restart(edges):
    edges.kwin = KWinStub()
    edges._reload_kwin_script()
    edges.configure("right", False)
    assert not edges.reload_timer.isActive()
    assert not edges.health_timer.isActive()
    edges._load_kwin_script()
    assert not any(call[0] == "loadScript" for call in edges.kwin.calls)
    edges.close()
    edges._check_kwin_script()
    assert not edges.reload_timer.isActive()
