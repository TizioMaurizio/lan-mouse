"""Text and PNG clipboard adapters. Wayland uses data-control via wl-clipboard."""

import hashlib
import os
import queue
import shutil
import subprocess
import threading
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, Signal
from PySide6.QtGui import QGuiApplication, QImage, QImageReader

from .core import MAX_CLIPBOARD, MAX_PIXELS, clipboard_digest

TEXT = "text/plain;charset=utf-8"
IMAGES = ("image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff")


class Clipboard(QObject):
    changed = Signal(str, bytes)
    warning = Signal(str)
    encoded = Signal(int, bytes, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.last = None
        self.applying = False
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.incoming = queue.Queue(maxsize=4)
        self.images = queue.Queue(maxsize=1)
        self.generation = 0
        self.last_pixels = None
        self.dirty = threading.Event()
        self.dirty.set()
        self.watcher = None
        self.watching = False
        self.watch_thread = None
        self.encoded.connect(self._qt_encoded)
        self.image_thread = threading.Thread(target=self._image_loop, daemon=True)
        self.image_thread.start()
        self.wayland = os.name != "nt" and bool(os.environ.get("WAYLAND_DISPLAY"))
        self.available = not self.wayland or bool(
            shutil.which("wl-copy") and shutil.which("wl-paste")
        )
        if self.wayland:
            if self.available:
                self._start_watch()
            threading.Thread(target=self._wayland_loop, daemon=True).start()
        else:
            self.clipboard = QGuiApplication.clipboard()
            self.clipboard.dataChanged.connect(self._qt_changed)

    def enable(self, enabled, publish_initial=False):
        with self.lock:
            self.enabled = enabled
            self.publish_initial = publish_initial
            self.last = None
            self.last_pixels = None
            self.generation += 1
            self.dirty.set()
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
        if self.closed.is_set():
            return
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

    @staticmethod
    def _png(image):
        if image.isNull() or image.width() * image.height() > MAX_PIXELS:
            raise ValueError("Clipboard image is invalid or exceeds 32 million pixels")
        array = QByteArray()
        buffer = QBuffer(array)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise ValueError("Could not encode clipboard image")
        return bytes(array)

    @staticmethod
    def _pixels(image):
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        return hashlib.sha256(image.constBits()).digest()

    @staticmethod
    def _decode_image(raw):
        if len(raw) > MAX_CLIPBOARD:
            raise ValueError("Clipboard exceeds 8 MiB; skipped")
        array = QByteArray(raw)
        buffer = QBuffer(array)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > MAX_PIXELS:
            raise ValueError("Clipboard image dimensions too large")
        return reader.read()

    def _qt_changed(self, initial=False):
        if not self.enabled or self.applying:
            return
        mime = self.clipboard.mimeData()
        if not mime:
            self.generation += 1
            self.last_pixels = None
            return
        offered = next((format for format in IMAGES if mime.hasFormat(format)), None)
        if mime.hasImage() or offered:
            try:
                image = (
                    self.clipboard.image()
                    if mime.hasImage()
                    else self._decode_image(bytes(mime.data(offered)))
                )
            except ValueError as exc:
                self.generation += 1
                self.last_pixels = None
                self.warning.emit(str(exc))
                return
            if image.isNull() or image.width() * image.height() > MAX_PIXELS:
                self.generation += 1
                self.last_pixels = None
                return
            pixels = self._pixels(image)
            if pixels == self.last_pixels:
                return
            self.generation += 1
            self.last_pixels = pixels
            # PNG compression runs outside Qt's GUI/input dispatch thread.
            try:
                self.images.get_nowait()
            except queue.Empty:
                pass
            self.images.put_nowait((self.generation, image.copy(), initial))
        elif not mime.hasUrls() and mime.hasText():
            self.generation += 1
            self.last_pixels = None
            self._publish(TEXT, mime.text().encode("utf-8"), initial)
        else:
            self.generation += 1
            self.last_pixels = None

    def _image_loop(self):
        while not self.closed.is_set():
            try:
                generation, image, initial = self.images.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                raw = self._png(image)
                if not self.closed.is_set():
                    self.encoded.emit(generation, raw, initial)
            except ValueError as exc:
                if not self.closed.is_set():
                    self.warning.emit(str(exc))

    def _qt_encoded(self, generation, raw, initial):
        if self.enabled and generation == self.generation:
            self._publish("image/png", raw, initial)

    def _watch_wayland(self):
        # wl-paste supplies each changed selection on stdin to this command.
        # Drain it without storing it; only a one-byte notification reaches Python.
        # The actual MIME is selected by _wayland_read, including mixed offers.
        try:
            if self.closed.is_set():
                return
            self.watcher = subprocess.Popen(
                ["wl-paste", "--watch", "sh", "-c", "cat >/dev/null; printf x"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.watching = True
            while not self.closed.is_set() and self.watcher.stdout.read(1):
                self.dirty.set()
        except OSError:
            pass
        finally:
            self.watching = False  # Poll on desktops lacking data-control watch.
            if self.watcher:
                if self.watcher.poll() is None:
                    self.watcher.terminate()
                self.watcher.wait()
                self.watcher.stdout.close()

    def _start_watch(self):
        self.watch_thread = threading.Thread(target=self._watch_wayland, daemon=True)
        self.watch_thread.start()

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
        for mime in IMAGES:
            if mime in types:
                raw = self._paste(mime)
                if mime == "image/png":
                    return mime, raw
                return "image/png", self._png(self._decode_image(raw))
        if "text/uri-list" in types:
            return None  # File-only offers are not copied as text paths.
        for mime in (TEXT, "text/plain", "UTF8_STRING"):
            if mime in types:
                raw = self._paste(mime)
                raw.decode("utf-8")
                return TEXT, raw
        return None

    def _wayland_loop(self):
        was_enabled = False
        last_poll = 0
        last_watch = time.monotonic()
        while not self.closed.wait(0.05):
            if not self.enabled or not self.available:
                was_enabled = False
                continue
            if time.monotonic() - last_watch > 5 and (
                self.watch_thread is None or not self.watch_thread.is_alive()
            ):
                # wl-paste --watch exits when its compositor connection disappears.
                # Reopen it after a desktop restart; use polling during recovery.
                last_watch = time.monotonic()
                self._start_watch()
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
                if self.watching:
                    if not self.dirty.is_set():
                        continue
                elif time.monotonic() - last_poll < 0.4:
                    continue
                self.dirty.clear()
                last_poll = time.monotonic()
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
                    self.generation += 1
                    self.last_pixels = self._pixels(image)
                    self.last = clipboard_digest(mime, raw)
                    # Advertise both Windows' native image and PNG for applications
                    # that prefer an explicit format (browsers, editors, chat apps).
                    content = QMimeData()
                    content.setImageData(image)
                    content.setData("image/png", QByteArray(raw))
                    self.clipboard.setMimeData(content)
                else:
                    self.generation += 1
                    self.last_pixels = None
                    self.last = clipboard_digest(mime, raw)
                    self.clipboard.setText(raw.decode("utf-8"))
            finally:
                self.applying = False

    def close(self):
        self.enabled = False
        self.closed.set()
        if self.watcher and self.watcher.poll() is None:
            self.watcher.terminate()
        self.image_thread.join(timeout=0.5)
