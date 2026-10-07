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
