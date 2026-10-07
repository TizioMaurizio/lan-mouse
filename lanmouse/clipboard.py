"""Text and PNG clipboard adapters. Wayland uses data-control via wl-clipboard."""

import os
import queue
import shutil
import subprocess
import threading

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage

from .core import MAX_CLIPBOARD, clipboard_digest

TEXT = "text/plain;charset=utf-8"


class Clipboard(QObject):
    changed = Signal(str, bytes)
    warning = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.last = None
        self.applying = False
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.incoming = queue.Queue(maxsize=4)
        self.wayland = os.name != "nt" and bool(os.environ.get("WAYLAND_DISPLAY"))
        self.available = not self.wayland or bool(
            shutil.which("wl-copy") and shutil.which("wl-paste")
        )
        if self.wayland:
            threading.Thread(target=self._wayland_loop, daemon=True).start()
        else:
            self.clipboard = QGuiApplication.clipboard()
            self.clipboard.dataChanged.connect(self._qt_changed)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._qt_changed)

    def enable(self, enabled, publish_initial=False):
        with self.lock:
            self.enabled = enabled
            self.publish_initial = publish_initial
            self.last = None
            if not enabled:
                while not self.incoming.empty():
                    try:
                        self.incoming.get_nowait()
                    except queue.Empty:
                        break
        if enabled and not self.wayland:
            self._qt_changed(initial=not publish_initial)
        if enabled and not self.available:
            self.warning.emit("Clipboard needs wl-clipboard. Run setup-linux.sh once.")

    def _publish(self, mime, data, initial=False):
        if len(data) > MAX_CLIPBOARD:
            self.warning.emit("Clipboard exceeds 8 MiB; skipped")
            return
        digest = clipboard_digest(mime, data)
        with self.lock:
            if digest == self.last:
                return
            self.last = digest
            if not self.enabled or initial:
                return
        self.changed.emit(mime, data)

    def _qt_changed(self, initial=False):
        if not self.enabled or self.applying:
            return
        mime = self.clipboard.mimeData()
        if not mime or mime.hasUrls():
            return
        if mime.hasImage():
            image = self.clipboard.image()
            if image.width() * image.height() > 32_000_000:
                return
            array = QByteArray()
            buffer = QBuffer(array)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            buffer.close()
            self._publish("image/png", bytes(array), initial)
        elif mime.hasText():
            self._publish(TEXT, mime.text().encode("utf-8"), initial)

    @staticmethod
    def _paste(mime):
        # Temporary-file output keeps a huge local clipboard out of process memory.
        import tempfile

        with tempfile.TemporaryFile() as output:
            subprocess.run(
                ["wl-paste", "--no-newline", "--type", mime],
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=2,
            )
            output.seek(0)
            data = output.read(MAX_CLIPBOARD + 1)
            if len(data) > MAX_CLIPBOARD:
                raise ValueError("Clipboard exceeds 8 MiB; skipped")
            return data

    def _wayland_read(self):
        types = (
            subprocess.run(["wl-paste", "--list-types"], capture_output=True, check=True, timeout=2)
            .stdout.decode()
            .splitlines()
        )
        if "text/uri-list" in types:
            return None  # Files are deliberately not treated as text paths.
        if "image/png" in types:
            return "image/png", self._paste("image/png")
        for mime in (TEXT, "text/plain", "UTF8_STRING"):
            if mime in types:
                raw = self._paste(mime)
                raw.decode("utf-8")
                return TEXT, raw
        return None

    def _wayland_loop(self):
        was_enabled = False
        while not self.closed.wait(0.4):
            if not self.enabled or not self.available:
                was_enabled = False
                continue
            try:
                try:
                    mime, raw = self.incoming.get_nowait()
                except queue.Empty:
                    pass
                else:
                    with self.lock:
                        subprocess.run(
                            ["wl-copy", "--type", mime],
                            input=raw,
                            stderr=subprocess.DEVNULL,
                            check=True,
                            timeout=2,
                        )
                        self.last = clipboard_digest(mime, raw)
                    was_enabled = True
                content = self._wayland_read()
                if content:
                    self._publish(*content, initial=not was_enabled and not self.publish_initial)
                was_enabled = True
            except (OSError, subprocess.SubprocessError):
                # An empty clipboard or a disappearing owner is normal on Wayland.
                continue
            except (ValueError, UnicodeError) as exc:
                self.warning.emit(str(exc))

    def apply(self, mime, raw):
        if not self.enabled:
            return
        if self.wayland:
            try:
                self.incoming.put_nowait((mime, raw))
            except queue.Full:
                self.warning.emit("Clipboard busy; try copying again")
        else:
            self.applying = True
            try:
                if mime == "image/png":
                    image = QImage.fromData(raw, "PNG")
                    if image.isNull():
                        raise ValueError("Could not decode clipboard image")
                    self.clipboard.setImage(image)
                else:
                    self.clipboard.setText(raw.decode("utf-8"))
            finally:
                self.applying = False
            self._qt_changed(initial=True)

    def close(self):
        self.enabled = False
        self.closed.set()
