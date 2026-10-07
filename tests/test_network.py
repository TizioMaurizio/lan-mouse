import queue
import socket
import threading
import time

import pytest

from lanmouse.core import ProtocolError, decode_clipboard, encode
from lanmouse.identity import Identity, fingerprint, verify_proof
from lanmouse.network import Network, Session, lan_address


def wait_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for condition")


class Peer:
    def __init__(self, path, name, approve=True):
        self.identity = Identity(path, name)
        self.approvals = []
        self.ready = queue.Queue()
        self.messages = queue.Queue()
        self.closed = queue.Queue()
        self.status = []
        self.approve = approve
        self.network = Network(
            self.identity,
            self._approve,
            self._ready,
            lambda session, message: self.messages.put(message),
            lambda session, reason: self.closed.put(reason),
            lambda peers: None,
            self.status.append,
            port=0,
            discovery=False,
        )
        self.network.start()

    def _approve(self, name, address):
        self.approvals.append((name, address))
        return self.approve

    def _ready(self, session):
        session.start()
        self.ready.put(session)

    def stop(self):
        self.network.stop()


@pytest.fixture
def peers(tmp_path):
    a, b = Peer(tmp_path / "a", "Linux"), Peer(tmp_path / "b", "Windows")
    try:
        yield a, b
    finally:
        a.stop()
        b.stop()


def test_mutual_pairing_encrypted_input_and_clipboard(peers):
    a, b = peers
    a.network.connect("127.0.0.1", b.network.port)
    client, server = a.ready.get(timeout=5), b.ready.get(timeout=5)
    assert client.sock.version() in {"TLSv1.2", "TLSv1.3"}
    assert client.peer_id == b.identity.id
    assert server.peer_id == a.identity.id
    assert a.identity.known(b.identity.der)
    assert b.identity.known(a.identity.der)
    assert len(a.approvals) == len(b.approvals) == 1
    message = {"type": "input", "kind": "key", "code": 30, "down": True}
    assert client.send(message)
    assert b.messages.get(timeout=3) == message
    clipboard = {"type": "clipboard", "mime": "text/plain;charset=utf-8", "data": "aGVsbG8="}
    assert server.send(clipboard)
    assert decode_clipboard(a.messages.get(timeout=3)) == (clipboard["mime"], b"hello")
    client.close("test")
    wait_until(lambda: server.closed.is_set())


def test_pairing_declined_has_no_session(peers):
    a, b = peers
    b.approve = False
    a.network.connect("127.0.0.1", b.network.port)
    wait_until(lambda: any("Could not connect" in status for status in a.status))
    assert a.network.session is None
    assert b.network.session is None
    assert not b.identity.known(a.identity.der)


def test_changed_certificate_rejected_without_new_approval(peers):
    a, b = peers
    a.network.connect("127.0.0.1", b.network.port, expected_id="0" * 64)
    wait_until(lambda: any("identity changed" in status for status in a.status))
    assert not a.approvals
    assert a.network.session is None


def test_reconnection_uses_saved_ip_and_needs_no_approval(peers):
    a, b = sorted(peers, key=lambda peer: peer.identity.id)
    a.network.connect("127.0.0.1", b.network.port)
    client, server = a.ready.get(timeout=5), b.ready.get(timeout=5)
    client.close()
    wait_until(lambda: server.closed.is_set())
    new_client, new_server = a.ready.get(timeout=7), b.ready.get(timeout=7)
    assert new_client is not client and new_server is not server
    assert len(a.approvals) == len(b.approvals) == 1


def test_identity_persists_and_proof_binds_to_nonce(tmp_path):
    a = Identity(tmp_path, "Test")
    b = Identity(tmp_path)
    assert a.id == b.id == fingerprint(a.der)
    challenge = b"first nonce and target fingerprint"
    signature = a.sign(challenge)
    verify_proof(a.der, signature, challenge)
    with pytest.raises(ProtocolError):
        verify_proof(a.der, signature, b"different nonce")


def test_stalled_frame_watchdog(monkeypatch):
    import lanmouse.network as network

    monkeypatch.setattr(network, "HEARTBEAT_TIMEOUT", 0.1)
    a, b = socket.socketpair()
    closed = threading.Event()
    session = Session(
        a, "peer", "PC", "127.0.0.1", True, lambda *args: None, lambda *args: closed.set()
    )
    session.start()
    try:
        b.sendall(encode({"type": "input"})[:6])
        assert closed.wait(3)
        assert session.closed.is_set()
    finally:
        session.close()
        b.close()


@pytest.mark.parametrize("address", ["8.8.8.8", "0.0.0.0", "224.0.0.1", "not-an-ip"])
def test_non_lan_addresses(address):
    assert not lan_address(address)


def test_movement_coalescing_preserves_clicks_and_control_stamps():
    a, b = socket.socketpair()
    session = Session(a, "peer", "PC", "local", True, lambda *args: None, lambda *args: None)
    try:

        def move(dx, dy, stamp=1):
            return {"type": "input", "kind": "move", "dx": dx, "dy": dy, "stamp": [stamp, "peer"]}

        for _ in range(1000):
            assert session.send(move(2, -1))
        assert len(session.outbox) == 1
        assert session.outbox[0] == move(2000, -1000)
        click = {"type": "input", "kind": "button", "code": 1, "down": True}
        session.send(click)
        session.send(move(3, 4))
        session.send(move(5, 6, stamp=2))
        assert list(session.outbox) == [move(2000, -1000), click, move(3, 4), move(5, 6, 2)]
        session.send(move(32767, 0, stamp=2))
        assert len(session.outbox) == 5  # Combined deltas must remain valid.
    finally:
        session.close()
        b.close()


def test_input_interleaves_with_large_clipboard_without_losing_content():
    from lanmouse.core import decode_clipboard, receive

    a, b = socket.socketpair()
    first_chunk = threading.Event()
    proceed = threading.Event()

    class PacedSocket:
        family = socket.AF_UNIX if hasattr(socket, "AF_UNIX") else -1

        def settimeout(self, timeout):
            a.settimeout(timeout)

        def sendall(self, data):
            a.sendall(data)
            if not first_chunk.is_set():
                first_chunk.set()
                assert proceed.wait(3)

        def shutdown(self, how):
            a.shutdown(how)

        def close(self):
            a.close()

    sender = Session(
        PacedSocket(), "peer", "PC", "local", True, lambda *args: None, lambda *args: None
    )
    receiver = Session(b, "peer", "PC", "local", False, lambda *args: None, lambda *args: None)
    raw = b"clipboard image-sized payload " * 100000
    clipboard = {
        "type": "clipboard",
        "mime": "text/plain;charset=utf-8",
        "data": raw,
        "stamp": [1, "peer"],
    }
    try:
        sender.send(clipboard)
        worker = threading.Thread(target=sender._write, daemon=True)
        worker.start()
        assert first_chunk.wait(3)
        part = receive(b)
        assert part["type"] == "clipboard_chunk"
        assert receiver._clipboard_part(part) is None
        key = {"type": "input", "kind": "key", "code": 30, "down": True}
        sender.send(key)
        proceed.set()
        # The key arrives ahead of the rest of the megabytes of clipboard data.
        assert receive(b) == key
        result = None
        while result is None:
            result = receiver._clipboard_part(receive(b))
        assert result["stamp"] == clipboard["stamp"]
        assert decode_clipboard(result) == (clipboard["mime"], raw)
    finally:
        proceed.set()
        sender.close()
        receiver.close()


def test_tcp_nodelay_enabled_on_paired_socket(peers):
    a, b = peers
    a.network.connect("127.0.0.1", b.network.port)
    for session in (a.ready.get(timeout=5), b.ready.get(timeout=5)):
        assert session.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"index": 1},
        {"size": 9 * 1024 * 1024},
        {"final": "yes"},
        {"data": "???"},
        {"data": "YQ==", "size": 0},
        {"size": 2},
    ],
)
def test_invalid_clipboard_chunks_rejected(change):
    a, b = socket.socketpair()
    session = Session(a, "peer", "PC", "local", True, lambda *args: None, lambda *args: None)
    try:
        part = {
            "type": "clipboard_chunk",
            "index": 0,
            "final": True,
            "mime": "text/plain;charset=utf-8",
            "data": "YQ==",
            "size": 1,
        }
        with pytest.raises(ProtocolError):
            session._clipboard_part({**part, **change})
    finally:
        session.close()
        b.close()


def test_large_image_survives_chunked_tls_transfer(peers, qt):
    import os

    from PySide6.QtGui import QImage

    from lanmouse.clipboard import Clipboard

    # Incompressible pixels exercise many chunks, rather than a tiny solid PNG.
    pixels = os.urandom(768 * 512 * 4)
    image = QImage(pixels, 768, 512, QImage.Format.Format_RGBA8888)
    raw = Clipboard._png(image)
    assert len(raw) > 1024 * 1024
    a, b = peers
    a.network.connect("127.0.0.1", b.network.port)
    sender = a.ready.get(timeout=5)
    b.ready.get(timeout=5)
    sender.send(
        {"type": "clipboard", "mime": "image/png", "data": raw, "stamp": [1, a.identity.id]}
    )
    result = b.messages.get(timeout=5)
    assert decode_clipboard(result) == ("image/png", raw)
    assert Clipboard._pixels(QImage.fromData(result["data"])) == Clipboard._pixels(image)


def test_discovery_prefers_actual_lan_over_link_local_and_vpn(monkeypatch):
    from types import SimpleNamespace

    import lanmouse.network as module

    monkeypatch.setattr(
        module.ifaddr,
        "get_adapters",
        lambda: [
            SimpleNamespace(
                ips=[
                    SimpleNamespace(ip="192.168.178.63", network_prefix=24),
                ]
            )
        ],
    )
    assert module.rank_addresses(["169.254.251.1", "10.99.0.1", "192.168.178.47"]) == [
        "192.168.178.47",
        "10.99.0.1",
        "169.254.251.1",
    ]


def test_saved_ip_is_retained_when_discovery_has_only_bad_adapter(peers, monkeypatch):
    import lanmouse.network as module

    a, b = sorted(peers, key=lambda peer: peer.identity.id)
    monkeypatch.setattr(module, "rank_addresses", lambda addresses: sorted(set(addresses)))
    discovered = [{"id": b.identity.id, "address": "169.254.251.1", "port": 45831}]
    known = {b.identity.id: {"address": "192.168.178.47", "port": 45831}}
    endpoints = a.network._reconnect_candidates(discovered, known)
    assert {item["address"] for item in endpoints} == {"169.254.251.1", "192.168.178.47"}


def test_reconnect_attempts_saved_endpoint_after_bad_discovery(peers, monkeypatch):
    import lanmouse.network as module

    a, b = sorted(peers, key=lambda peer: peer.identity.id)
    # Reconnect connects a real TLS peer even when its mDNS advert points elsewhere.
    a.identity.remember(b.identity.der, "127.0.0.1", b.network.port)
    b.identity.remember(a.identity.der, "127.0.0.1", a.network.port)
    a.network.peers["bad-adapter"] = {
        "id": b.identity.id,
        "address": "169.254.251.1",
        "port": 45831,
    }
    original = a.network._connect
    attempts = []
    monkeypatch.setattr(
        module,
        "rank_addresses",
        lambda addresses: sorted(set(addresses), key=lambda address: address != "169.254.251.1"),
    )

    def connect(address, port, peer_id, interactive):
        attempts.append(address)
        if address != "169.254.251.1":
            original(address, port, peer_id, interactive)

    monkeypatch.setattr(a.network, "_connect", connect)
    client = a.ready.get(timeout=7)
    server = b.ready.get(timeout=7)
    assert client.peer_id == b.identity.id and server.peer_id == a.identity.id
    assert attempts[:2] == ["169.254.251.1", "127.0.0.1"]
