"""Windows hooks for capture and SendInput for physical-key injection."""

import ctypes
import threading
import time
from ctypes import wintypes

from pynput import keyboard, mouse

from ..keycodes import F8, REVERSE_SCAN, SCAN

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", INPUTUNION)]


USER32 = ctypes.WinDLL("user32", use_last_error=True)
USER32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
USER32.SendInput.restype = wintypes.UINT
USER32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


class WindowsInput:
    def __init__(self, emit, toggle, error):
        self.emit, self.toggle, self.error = emit, toggle, error
        self.active = self.receiving = self.enabled = False
        self.ready = True
        self.lease = time.monotonic()
        self.lock = threading.RLock()
        self.pressed = set()
        self.physical_keys = set()
        self.origin = None
        self.closed = threading.Event()
        self.keyboard = keyboard.Listener(win32_event_filter=self._keyboard)
        self.mouse = mouse.Listener(win32_event_filter=self._mouse)
        self.keyboard.start()
        self.mouse.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def set_active(self, active):
        if active and not self.active:
            if self.physical_keys - {F8} or any(
                USER32.GetAsyncKeyState(button) & 0x8000 for button in (1, 2, 4, 5, 6)
            ):
                raise RuntimeError("Release held keys and mouse buttons before switching")
            point = wintypes.POINT()
            USER32.GetCursorPos(ctypes.byref(point))
            self.origin = (point.x, point.y)
            # Suppressed hook positions are clipped to the desktop. An interior anchor
            # preserves outward movement when sharing from a screen edge.
            USER32.SetCursorPos(USER32.GetSystemMetrics(0) // 2, USER32.GetSystemMetrics(1) // 2)
        self.active = active
        if not active and self.origin:
            origin, self.origin = self.origin, None
            USER32.SetCursorPos(*origin)
        self.lease = time.monotonic()

    def _keyboard(self, msg, data):
        if data.flags & 0x10:  # LLKHF_INJECTED: never forward injected input.
            return False
        down = msg in (0x100, 0x104)
        code = REVERSE_SCAN.get((data.scanCode, bool(data.flags & 1)))
        repeat = down and code in self.physical_keys
        if code:
            self.physical_keys.add(code) if down else self.physical_keys.discard(code)
        if code == F8 and self.enabled:
            if down:
                # Windows emits repeated key-down events; one toggle per physical press.
                if not getattr(self, "f8_down", False):
                    self.f8_down = True
                    self.set_active(False)
                    self.toggle()
            else:
                self.f8_down = False
            self.keyboard.suppress_event()
        elif self.active:
            if code:
                self.emit(
                    {"type": "input", "kind": "key", "code": code, "down": down, "repeat": repeat}
                )
            self.keyboard.suppress_event()
        return False

    def _mouse(self, msg, data):
        if data.flags & 1 or not self.active:  # LLMHF_INJECTED
            return False
        message = None
        if msg == 0x200:
            point = wintypes.POINT()
            USER32.GetCursorPos(ctypes.byref(point))
            # Suppressed motion never changes the local pointer: compare against its anchor.
            message = {
                "type": "input",
                "kind": "move",
                "dx": max(-32768, min(32768, data.pt.x - point.x)),
                "dy": max(-32768, min(32768, data.pt.y - point.y)),
            }
        elif msg in (0x20A, 0x20E):
            amount = ctypes.c_short(data.mouseData >> 16).value // 120
            message = {
                "type": "input",
                "kind": "scroll",
                "dx": amount if msg == 0x20E else 0,
                "dy": amount if msg == 0x20A else 0,
            }
        else:
            buttons = {
                0x201: (1, True),
                0x202: (1, False),
                0x204: (2, True),
                0x205: (2, False),
                0x207: (3, True),
                0x208: (3, False),
            }
            if msg in (0x20B, 0x20C):
                button = 4 if (data.mouseData >> 16) == 1 else 5
                buttons[msg] = (button, msg == 0x20B)
            if msg in buttons:
                code, down = buttons[msg]
                message = {"type": "input", "kind": "button", "code": code, "down": down}
        if message:
            self.emit(message)
        self.mouse.suppress_event()
        return False

    def _send(self, item):
        if USER32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT)) != 1:
            raise OSError(
                "Windows blocked input. For an elevated target, run LAN Mouse as administrator."
            )

    def inject(self, message):
        with self.lock:
            kind = message["kind"]
            if kind == "key":
                scan = SCAN.get(message["code"])
                if not scan:
                    return
                code, extended = scan
                flags = 8 | int(extended) | (0 if message["down"] else 2)
                item = INPUT(type=1, ki=KEYBDINPUT(0, code, flags, 0, 0))
            elif kind == "button":
                code, down = message["code"], message["down"]
                flags = {1: (2, 4), 2: (8, 16), 3: (32, 64), 4: (128, 256), 5: (128, 256)}[code][
                    0 if down else 1
                ]
                data = (code - 3) if code >= 4 else 0
                item = INPUT(type=0, mi=MOUSEINPUT(0, 0, data, flags, 0, 0))
            elif kind == "move":
                item = INPUT(type=0, mi=MOUSEINPUT(message["dx"], message["dy"], 0, 1, 0, 0))
            else:
                for axis, flags in (("dx", 0x1000), ("dy", 0x800)):
                    if message[axis]:
                        self._send(
                            INPUT(
                                type=0,
                                mi=MOUSEINPUT(
                                    0, 0, (message[axis] * 120) & 0xFFFFFFFF, flags, 0, 0
                                ),
                            )
                        )
                return
            self._send(item)
            if kind in ("key", "button"):
                pressed = (kind, message["code"])
                self.pressed.add(pressed) if message["down"] else self.pressed.discard(pressed)

    def release_all(self):
        with self.lock:
            for kind, code in list(self.pressed):
                try:
                    self.inject({"kind": kind, "code": code, "down": False})
                except OSError:
                    pass
            self.pressed.clear()

    def _watchdog(self):
        while not self.closed.wait(0.5):
            if time.monotonic() - self.lease > 3 and (self.active or self.receiving):
                self.set_active(False)
                self.receiving = False
                self.release_all()
                self.error("App stopped responding; local control restored")
            if not self.keyboard.is_alive() or not self.mouse.is_alive():
                self.ready = False
                self.set_active(False)
                self.error("Windows input hooks stopped; restart LAN Mouse")
                return

    def close(self):
        self.set_active(False)
        self.closed.set()
        self.release_all()
        self.keyboard.stop()
        self.mouse.stop()
