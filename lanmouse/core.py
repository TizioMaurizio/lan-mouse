"""Wire format and deterministic state; no desktop dependencies."""

import base64
import hashlib
import json
import struct

PORT = 45831
VERSION = 1
MAX_CLIPBOARD = 8 * 1024 * 1024
MAX_FRAME = 12 * 1024 * 1024
MAX_PIXELS = 32_000_000
MIMES = {"text/plain;charset=utf-8", "image/png"}


class ProtocolError(ValueError):
    pass


def encode(message):
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME:
        raise ProtocolError("Message too large")
    return struct.pack("!I", len(payload)) + payload


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(min(size - len(result), 65536))
        if not chunk:
            raise EOFError("Connection closed")
        result.extend(chunk)
    return bytes(result)


def receive(sock, limit=MAX_FRAME):
    size = struct.unpack("!I", read_exact(sock, 4))[0]
    if not 0 < size <= limit:
        raise ProtocolError("Invalid frame length")
    try:
        obj = json.loads(read_exact(sock, size))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid message") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("Invalid message object")
    return obj


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError("Invalid numeric value")
    return value


def validate_input(message):
    kind = message.get("kind")
    if kind == "move":
        return {
            "type": "input",
            "kind": kind,
            "dx": integer(message.get("dx"), -32768, 32768),
            "dy": integer(message.get("dy"), -32768, 32768),
        }
    if kind == "scroll":
        return {
            "type": "input",
            "kind": kind,
            "dx": integer(message.get("dx"), -120, 120),
            "dy": integer(message.get("dy"), -120, 120),
        }
    if kind in {"key", "button"}:
        code = integer(message.get("code"), 1, 248 if kind == "key" else 5)
        down = message.get("down")
        if type(down) is not bool:
            raise ProtocolError("Invalid key state")
        result = {"type": "input", "kind": kind, "code": code, "down": down}
        if kind == "key":
            repeat = message.get("repeat", False)
            if type(repeat) is not bool:
                raise ProtocolError("Invalid repeat state")
            result["repeat"] = repeat
        return result
    raise ProtocolError("Unknown input kind")


class Clock:
    """Lamport stamps resolve simultaneous changes in the same way on both PCs."""

    def __init__(self, node):
        self.node = node
        self.counter = 0
        self.current = (0, "")

    def tick(self):
        self.counter += 1
        self.current = (self.counter, self.node)
        return list(self.current)

    def accept(self, stamp, peer):
        if (
            not isinstance(stamp, list)
            or len(stamp) != 2
            or stamp[1] != peer
            or not isinstance(stamp[1], str)
        ):
            raise ProtocolError("Invalid state stamp")
        number = integer(stamp[0], 1, 2**53)
        self.counter = max(self.counter, number)
        incoming = (number, peer)
        if incoming <= self.current:
            return False
        self.current = incoming
        return True


def clipboard_digest(mime, data):
    return hashlib.sha256(mime.encode() + b"\0" + data).hexdigest()


def decode_clipboard(message):
    mime = message.get("mime")
    data = message.get("data")
    if mime not in MIMES or not isinstance(data, str) or len(data) > MAX_CLIPBOARD * 4 // 3 + 4:
        raise ProtocolError("Unsupported or oversized clipboard")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise ProtocolError("Invalid clipboard encoding") from exc
    if len(raw) > MAX_CLIPBOARD:
        raise ProtocolError("Clipboard too large")
    if mime.startswith("text/"):
        raw.decode("utf-8")
    elif len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ProtocolError("Invalid PNG")
    else:
        width, height = struct.unpack("!II", raw[16:24])
        if not width or not height or width * height > MAX_PIXELS:
            raise ProtocolError("Image dimensions too large")
    return mime, raw
