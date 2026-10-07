import base64
import socket
import struct

import pytest

from lanmouse.core import (
    MAX_FRAME,
    Clock,
    ProtocolError,
    clipboard_digest,
    decode_clipboard,
    encode,
    receive,
    validate_input,
)
from lanmouse.keycodes import REVERSE_SCAN, SCAN


class Fragmented:
    def __init__(self, data):
        self.data = data

    def recv(self, size):
        chunk, self.data = self.data[: min(size, 3)], self.data[min(size, 3) :]
        return chunk


def test_fragmented_unicode_frame():
    message = {"type": "clipboard", "text": "caffè ☕\nsecond line"}
    assert receive(Fragmented(encode(message))) == message


@pytest.mark.parametrize("payload", [b"[]", b"{}", b"null", b"invalid", b'{"type":1}'])
def test_invalid_message(payload):
    with pytest.raises(ProtocolError):
        receive(Fragmented(struct.pack("!I", len(payload)) + payload))


def test_frame_length_rejected_before_payload_read():
    with pytest.raises(ProtocolError):
        receive(Fragmented(struct.pack("!I", MAX_FRAME + 1)))


def test_truncated_frame():
    with pytest.raises(EOFError):
        receive(Fragmented(encode({"type": "ping"})[:-2]))


def test_real_socket_frames():
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(encode({"type": "ping"}) + encode({"type": "test", "value": 7}))
        assert receive(receiver) == {"type": "ping"}
        assert receive(receiver)["value"] == 7
    finally:
        sender.close()
        receiver.close()


def test_concurrent_changes_converge():
    a, b = Clock("a"), Clock("b")
    stamp_a, stamp_b = a.tick(), b.tick()
    assert a.accept(stamp_b, "b")
    assert not b.accept(stamp_a, "a")
    assert a.current == b.current
    assert not a.accept(stamp_b, "b")  # Replay cannot change state.
    next_a = a.tick()
    assert b.accept(next_a, "a")
    assert a.current == b.current


def test_forged_state_owner():
    with pytest.raises(ProtocolError):
        Clock("a").accept([1, "a"], "b")


@pytest.mark.parametrize(
    "message",
    [
        {"kind": "key", "code": True, "down": True},
        {"kind": "key", "code": 30, "down": 1},
        {"kind": "button", "code": 6, "down": True},
        {"kind": "move", "dx": 999999, "dy": 0},
        {"kind": "shell", "command": "something"},
    ],
)
def test_invalid_input(message):
    with pytest.raises(ProtocolError):
        validate_input(message)


def test_text_clipboard_roundtrip():
    data = "Àèìòù\n你好\n🙂".encode()
    mime, actual = decode_clipboard(
        {"mime": "text/plain;charset=utf-8", "data": base64.b64encode(data).decode()}
    )
    assert actual == data
    assert clipboard_digest(mime, data) == clipboard_digest(mime, actual)


def test_clipboard_disallows_paths_and_invalid_base64():
    for message in ({"mime": "text/uri-list", "data": ""}, {"mime": "image/png", "data": "!!!"}):
        with pytest.raises(ProtocolError):
            decode_clipboard(message)


def test_png_pixel_limit():
    header = (
        b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", 100000, 100000)
    )
    with pytest.raises(ProtocolError):
        decode_clipboard({"mime": "image/png", "data": base64.b64encode(header).decode()})


def test_physical_key_mapping():
    for code in [30, 66, 86, 87, 88, 96, 97, 100, 103, 108, 111, 125, 126]:
        assert REVERSE_SCAN[SCAN[code]] == code
