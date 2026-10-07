import queue
import socket
import threading
import time

import pytest

from lanmouse.core import ProtocolError, encode
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
    assert a.messages.get(timeout=3) == clipboard
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
