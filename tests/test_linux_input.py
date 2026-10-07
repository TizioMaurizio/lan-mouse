import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Linux evdev backend", allow_module_level=True)

from evdev import InputEvent
from evdev import ecodes as e

from lanmouse.backends.linux import LinuxInput


class Device:
    def __init__(self, path, name="Physical keyboard", fail=False):
        self.path, self.name, self.fail = path, name, fail
        self.grabbed = False
        self.keys = []
        self.events = []

    def capabilities(self):
        return {e.EV_KEY: [e.KEY_A, e.KEY_F8], e.EV_REL: [e.REL_X, e.REL_Y]}

    def active_keys(self):
        return self.keys

    def grab(self):
        if self.fail:
            raise OSError("busy")
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        self.grabbed = False

    def write(self, *args):
        self.events.append(args)

    def syn(self):
        pass


@pytest.fixture
def backend(monkeypatch):
    import lanmouse.backends.linux as linux

    devices = {
        "a": Device("a"),
        "b": Device("b"),
        "virtual": Device("virtual", name="LAN Mouse Python Keyboard"),
    }
    monkeypatch.setattr(linux.evdev, "list_devices", lambda: sorted(devices))
    monkeypatch.setattr(linux.evdev, "InputDevice", lambda path: devices[path])
    monkeypatch.setattr(linux.evdev, "UInput", lambda *args, **kwargs: Device("uinput"))
    monkeypatch.setattr(LinuxInput, "_run", lambda self: None)
    events, toggles, errors = [], [], []
    value = LinuxInput(events.append, lambda: toggles.append(True), errors.append)
    value.enabled = True
    yield value, devices, events, toggles, errors
    value.close()


def test_virtual_devices_never_recaptured(backend):
    value, *_ = backend
    assert set(value.devices) == {"a", "b"}


def test_grab_rollback_on_partial_failure(backend):
    value, devices, *_ = backend
    devices["b"].fail = True
    with pytest.raises(OSError):
        value.set_active(True)
    assert not value.active
    assert not devices["a"].grabbed


def test_held_keys_prevent_switch(backend):
    value, devices, *_ = backend
    devices["a"].keys = [e.KEY_LEFTCTRL]
    with pytest.raises(RuntimeError, match="Release"):
        value.set_active(True)
    assert not value.active


def test_hotkey_releases_grabs_before_gui_callback(backend):
    value, devices, events, toggles, _ = backend
    value.set_active(True)
    value._event(InputEvent(0, 0, e.EV_KEY, e.KEY_F8, 1))
    assert toggles == [True]
    assert not events
    assert not value.active
    assert not any(device.grabbed for device in devices.values())


def test_injected_keys_released_on_disconnect(backend):
    value, *_ = backend
    value.inject({"kind": "key", "code": e.KEY_LEFTCTRL, "down": True})
    value.inject({"kind": "button", "code": 1, "down": True})
    value.release_all()
    assert value.keyboard.events[-1] == (e.EV_KEY, e.KEY_LEFTCTRL, 0)
    assert value.mouse.events[-1] == (e.EV_KEY, e.BTN_LEFT, 0)
    assert not value.pressed


def test_raw_mouse_scroll_and_physical_keys(backend):
    value, _, events, *_ = backend
    value.set_active(True)
    for kind, code, state in [
        (e.EV_REL, e.REL_X, 12),
        (e.EV_REL, e.REL_WHEEL, -1),
        (e.EV_KEY, e.KEY_A, 1),
        (e.EV_KEY, e.KEY_A, 0),
    ]:
        value._event(InputEvent(0, 0, kind, code, state))
    assert [event["kind"] for event in events] == ["move", "scroll", "key", "key"]
    assert events[0]["dx"] == 12
    assert events[1]["dy"] == -1


def test_missing_uinput_permission_reports_setup(monkeypatch):
    import lanmouse.backends.linux as linux

    def denied(*args, **kwargs):
        raise linux.evdev.UInputError("permission denied")

    monkeypatch.setattr(linux.evdev, "UInput", denied)
    monkeypatch.setattr(LinuxInput, "_run", lambda self: None)
    errors = []
    value = LinuxInput(lambda event: None, lambda: None, errors.append)
    try:
        assert not value.ready
        assert "setup-linux.sh" in errors[0]
    finally:
        value.close()


def test_linux_repeat_forwarded_but_not_reinjected(backend):
    value, _, events, *_ = backend
    value.set_active(True)
    value._event(InputEvent(0, 0, e.EV_KEY, e.KEY_A, 2))
    assert events[-1]["repeat"] is True
    value.inject(events[-1])
    assert not value.keyboard.events
