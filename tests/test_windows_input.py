import ctypes
import sys
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip("Windows input backend (runs on Windows CI)", allow_module_level=True)

from lanmouse.backends.windows import INPUT, WindowsInput


class Suppressed(Exception):
    pass


class Listener:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def is_alive(self):
        return True

    def suppress_event(self):
        raise Suppressed


class User32:
    def __init__(self):
        self.sent = []
        self.position = (100, 200)

    def SendInput(self, count, pointer, size):
        value = ctypes.cast(pointer, ctypes.POINTER(INPUT)).contents
        self.sent.append(bytes(value))
        assert size == ctypes.sizeof(INPUT)
        return 1

    def GetCursorPos(self, pointer):
        pointer._obj.x, pointer._obj.y = self.position
        return 1

    def SetCursorPos(self, x, y):
        self.position = (x, y)
        return 1

    def GetSystemMetrics(self, metric):
        return 1920 if metric == 0 else 1080

    def GetAsyncKeyState(self, key):
        return 0


@pytest.fixture
def backend(monkeypatch):
    import lanmouse.backends.windows as windows

    api = User32()
    monkeypatch.setattr(windows, "USER32", api)
    monkeypatch.setattr(windows.keyboard, "Listener", Listener)
    monkeypatch.setattr(windows.mouse, "Listener", Listener)
    events, toggles = [], []
    value = WindowsInput(events.append, lambda: toggles.append(True), lambda text: None)
    value.enabled = True
    yield value, api, events, toggles
    value.close()


def test_native_structure_abi():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_scan_injection_and_release(backend):
    value, api, *_ = backend
    value.inject({"kind": "key", "code": 97, "down": True})
    down = INPUT.from_buffer_copy(api.sent[-1])
    assert down.type == 1 and down.ki.wScan == 0x1D and down.ki.dwFlags == 9
    value.release_all()
    up = INPUT.from_buffer_copy(api.sent[-1])
    assert up.ki.dwFlags == 11


def test_remote_mouse_anchor_and_restore(backend):
    value, api, events, _ = backend
    origin = api.position
    value.set_active(True)
    assert api.position == (960, 540)
    data = SimpleNamespace(flags=0, pt=SimpleNamespace(x=980, y=532), mouseData=0)
    with pytest.raises(Suppressed):
        value._mouse(0x200, data)
    assert events[-1]["dx"] == 20 and events[-1]["dy"] == -8
    value.set_active(False)
    assert api.position == origin


def test_f8_repeat_toggles_once_and_restores_input(backend):
    value, _, _, toggles = backend
    value.set_active(True)
    data = SimpleNamespace(flags=0, scanCode=66)
    for _ in range(2):
        with pytest.raises(Suppressed):
            value._keyboard(0x100, data)
    assert toggles == [True]
    assert not value.active


def test_injected_events_are_never_forwarded(backend):
    value, _, events, _ = backend
    value.set_active(True)
    assert value._keyboard(0x100, SimpleNamespace(flags=0x10)) is False
    assert value._mouse(0x200, SimpleNamespace(flags=1)) is False
    assert not events
