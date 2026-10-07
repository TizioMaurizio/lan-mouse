"""evdev capture and uinput injection, independent of X11 or Wayland."""

import select
import threading
import time

import evdev
from evdev import ecodes as e

from ..keycodes import F8, SCAN

PREFIX = "LAN Mouse Python"
BUTTONS = {e.BTN_LEFT: 1, e.BTN_RIGHT: 2, e.BTN_MIDDLE: 3, e.BTN_SIDE: 4, e.BTN_EXTRA: 5}
REVERSE_BUTTONS = {v: k for k, v in BUTTONS.items()}


class LinuxInput:
    def __init__(self, emit, toggle, error):
        self.emit, self.toggle, self.error = emit, toggle, error
        self.lock = threading.RLock()
        self.active = False
        self.enabled = False
        self.receiving = False
        self.lease = time.monotonic()
        self.devices = {}
        self.pressed = set()
        self.closed = threading.Event()
        self.keyboard = self.mouse = None
        self.ready = False
        try:
            self.keyboard = evdev.UInput({e.EV_KEY: sorted(SCAN)}, name=f"{PREFIX} Keyboard")
            self.mouse = evdev.UInput(
                {
                    e.EV_KEY: sorted(BUTTONS),
                    e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
                },
                name=f"{PREFIX} Mouse",
            )
            self._scan()
            if not any(
                e.KEY_A in d.capabilities().get(e.EV_KEY, []) for d in self.devices.values()
            ):
                raise PermissionError("No accessible physical keyboard")
            self.ready = True
        except (OSError, ValueError, evdev.UInputError) as exc:
            self.error(f"Linux input needs setup: {exc}. Run setup-linux.sh once, then restart.")
        threading.Thread(target=self._run, daemon=True).start()

    def _scan(self):
        with self.lock:
            paths = set(evdev.list_devices())
            for path in list(self.devices):
                if path not in paths:
                    self.set_active(False)
                    self.devices.pop(path).close()
                    self.error("Input device removed; local control restored")
            for path in paths - self.devices.keys():
                try:
                    device = evdev.InputDevice(path)
                    caps = device.capabilities()
                    keyboard = e.KEY_A in caps.get(e.EV_KEY, [])
                    mouse = e.REL_X in caps.get(e.EV_REL, [])
                    if device.name.startswith(PREFIX) or not (keyboard or mouse):
                        device.close()
                        continue
                    if self.active:
                        self.set_active(False)
                        self.error("Input device added; press F8 to resume sharing")
                    self.devices[path] = device
                except OSError:
                    continue

    def set_active(self, active):
        with self.lock:
            if active == self.active:
                return
            if active:
                if not self.ready:
                    raise RuntimeError("Linux input is not ready; run setup-linux.sh first")
                grabbed = []
                try:
                    for device in self.devices.values():
                        if any(key != F8 for key in device.active_keys()):
                            raise RuntimeError(
                                "Release held keys and mouse buttons before switching"
                            )
                        device.grab()
                        grabbed.append(device)
                except Exception:
                    for device in grabbed:
                        device.ungrab()
                    raise
                self.lease = time.monotonic()
                self.active = True
            else:
                self.active = False
                for device in self.devices.values():
                    try:
                        device.ungrab()
                    except OSError:
                        pass

    def _run(self):
        last_scan = time.monotonic()
        while not self.closed.wait(0.005):
            try:
                if time.monotonic() - last_scan > 2:
                    self._scan()
                    last_scan = time.monotonic()
                if time.monotonic() - self.lease > 3 and (self.active or self.receiving):
                    self.set_active(False)
                    self.release_all()
                    self.receiving = False
                    self.error("App stopped responding; local control restored")
                with self.lock:
                    devices = list(self.devices.values())
                if not devices:
                    continue
                readable, _, _ = select.select(devices, [], [], 0.1)
                for device in readable:
                    with self.lock:
                        events = list(device.read())
                    for event in events:
                        self._event(event)
            except BlockingIOError:
                continue
            except Exception as exc:
                self.set_active(False)
                self.release_all()
                self.error(f"Input stopped: {exc}")
                if self.closed.wait(1):
                    return

    def _event(self, event):
        if event.type == e.EV_SYN and event.code == e.SYN_DROPPED:
            self.set_active(False)
            self.error("Input buffer overflow; local control restored")
            return
        if event.type == e.EV_KEY and event.code == F8:
            if event.value == 1 and self.enabled:
                # Release directly in the capture thread even if the GUI is hung.
                if self.active:
                    self.set_active(False)
                self.toggle()
            return
        if not self.active:
            return
        message = None
        if event.type == e.EV_REL:
            if event.code in (e.REL_X, e.REL_Y):
                message = {
                    "type": "input",
                    "kind": "move",
                    "dx": event.value if event.code == e.REL_X else 0,
                    "dy": event.value if event.code == e.REL_Y else 0,
                }
            elif event.code in (e.REL_WHEEL, e.REL_HWHEEL):
                message = {
                    "type": "input",
                    "kind": "scroll",
                    "dx": event.value if event.code == e.REL_HWHEEL else 0,
                    "dy": event.value if event.code == e.REL_WHEEL else 0,
                }
        elif event.type == e.EV_KEY:
            if event.code in BUTTONS:
                message = {
                    "type": "input",
                    "kind": "button",
                    "code": BUTTONS[event.code],
                    "down": bool(event.value),
                }
            elif event.code in SCAN:
                message = {
                    "type": "input",
                    "kind": "key",
                    "code": event.code,
                    "down": bool(event.value),
                    "repeat": event.value == 2,
                }
        if message:
            self.emit(message)

    def inject(self, message):
        with self.lock:
            if not self.ready:
                raise RuntimeError("Linux input is not ready")
            kind = message["kind"]
            if kind in ("key", "button"):
                if kind == "key" and message.get("repeat"):
                    return  # X11/Wayland repeat from the held uinput key themselves.
                code = message["code"] if kind == "key" else REVERSE_BUTTONS[message["code"]]
                if kind == "key" and code not in SCAN:
                    return
                device = self.keyboard if kind == "key" else self.mouse
                device.write(e.EV_KEY, code, int(message["down"]))
                device.syn()
                key = (kind, code)
                self.pressed.add(key) if message["down"] else self.pressed.discard(key)
            else:
                codes = (e.REL_X, e.REL_Y) if kind == "move" else (e.REL_HWHEEL, e.REL_WHEEL)
                self.mouse.write(e.EV_REL, codes[0], message["dx"])
                self.mouse.write(e.EV_REL, codes[1], message["dy"])
                self.mouse.syn()

    def release_all(self):
        with self.lock:
            for kind, code in list(self.pressed):
                try:
                    device = self.keyboard if kind == "key" else self.mouse
                    device.write(e.EV_KEY, code, 0)
                    device.syn()
                except OSError:
                    pass
            self.pressed.clear()

    def close(self):
        self.closed.set()
        self.set_active(False)
        self.release_all()
        with self.lock:
            for device in self.devices.values():
                device.close()
            self.devices.clear()
            for device in (self.mouse, self.keyboard):
                if device:
                    device.close()
