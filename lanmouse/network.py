"""Discovery, mutually authenticated pairing, bounded queues and reconnects."""

import base64
import ipaddress
import queue
import secrets
import socket
import ssl
import threading
import time

import ifaddr
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, Zeroconf

from .core import PORT, VERSION, ProtocolError, encode, receive
from .identity import certificate_name, fingerprint, verify_proof

SERVICE = "_lanmousepy._tcp.local."
HEARTBEAT_TIMEOUT = 8


def lan_address(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_private and not ip.is_unspecified and not ip.is_multicast
    except ValueError:
        return False


def local_addresses():
    result = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and lan_address(ip.ip) and not ip.ip.startswith("127."):
                result.add(ip.ip)
    return sorted(result)


class Session:
    def __init__(self, sock, peer_id, name, address, initiator, on_message, on_close):
        self.sock, self.peer_id, self.name = sock, peer_id, name
        self.address, self.initiator = address, initiator
        self.on_message, self.on_close = on_message, on_close
        self.outbox = queue.Queue(maxsize=1024)
        self.closed = threading.Event()
        self.last_received = time.monotonic()
        self.sock.settimeout(HEARTBEAT_TIMEOUT)
        self.close_lock = threading.Lock()

    def start(self):
        for target in (self._read, self._write, self._heartbeat):
            threading.Thread(target=target, daemon=True).start()

    def send(self, message):
        if self.closed.is_set():
            return False
        try:
            self.outbox.put_nowait(message)
            return True
        except queue.Full:
            self.close("Connection too slow; local control restored")
            return False

    def _read(self):
        try:
            while not self.closed.is_set():
                message = receive(self.sock)
                self.last_received = time.monotonic()
                if message["type"] != "ping":
                    self.on_message(self, message)
        except (OSError, EOFError, ValueError) as exc:
            self.close(f"Disconnected: {exc}")

    def _write(self):
        try:
            while not self.closed.is_set():
                try:
                    message = self.outbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                self.sock.sendall(encode(message))
        except (OSError, ValueError) as exc:
            self.close(f"Disconnected: {exc}")

    def _heartbeat(self):
        # Also handles a peer that sends a frame header then never finishes its body.
        while not self.closed.wait(2):
            if time.monotonic() - self.last_received > HEARTBEAT_TIMEOUT:
                self.close("Connection lost; local control restored")
                return
            self.send({"type": "ping"})

    def close(self, reason="Disconnected"):
        with self.close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        self.on_close(self, reason)


class Network:
    def __init__(
        self,
        identity,
        approve,
        on_ready,
        on_message,
        on_close,
        on_peers,
        on_status,
        port=PORT,
        discovery=True,
    ):
        self.identity = identity
        self.approve = approve
        self.on_ready, self.on_message, self.on_close = on_ready, on_message, on_close
        self.on_peers, self.on_status = on_peers, on_status
        self.port = port
        self.discovery = discovery
        self.discovery_thread = None
        self.session = None
        self.peers = {}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.connecting = threading.Lock()
        self.pending = threading.BoundedSemaphore(2)
        self.paused = False
        self.listener = None
        self.zeroconf = self.browser = self.info = None
        self.context = identity.server_context()

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("0.0.0.0", self.port))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(4)
        self.listener.settimeout(1)
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._reconnect, daemon=True).start()
        if self.discovery:
            self.discovery_thread = threading.Thread(target=self._discover, daemon=True)
            self.discovery_thread.start()

    def _discover(self):
        try:
            self.zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            self.info = ServiceInfo(
                SERVICE,
                f"{self.identity.id[:24]}.{SERVICE}",
                addresses=[socket.inet_aton(a) for a in local_addresses()],
                port=self.port,
                properties={
                    "id": self.identity.id,
                    "name": self.identity.name,
                    "version": str(VERSION),
                },
                server=f"lanmouse-{self.identity.id[:24]}.local.",
            )
            self.zeroconf.register_service(self.info)
            if self.stopped.is_set():
                return
            self.browser = ServiceBrowser(self.zeroconf, SERVICE, listener=self)
        except Exception as exc:
            self.on_status(f"Discovery unavailable ({exc}); use Connect by IP")

    def add_service(self, zc, service_type, name):
        self.update_service(zc, service_type, name)

    def update_service(self, zc, service_type, name):
        info = zc.get_service_info(service_type, name, timeout=1500)
        if not info:
            return
        peer_id = info.properties.get(b"id", b"").decode(errors="replace")
        if peer_id == self.identity.id or len(peer_id) != 64:
            return
        addresses = [a for a in info.parsed_addresses() if lan_address(a)]
        if not addresses:
            return
        label = info.properties.get(b"name", b"Computer").decode(errors="replace")[:80]
        label = "".join(c for c in label if c.isprintable())
        with self.lock:
            self.peers[name] = {
                "id": peer_id,
                "name": label,
                "address": addresses[0],
                "port": info.port,
            }
            self.on_peers(list(self.peers.values()))

    def remove_service(self, zc, service_type, name):
        with self.lock:
            self.peers.pop(name, None)
            self.on_peers(list(self.peers.values()))

    def _trust(self, der, address, interactive):
        if fingerprint(der) == self.identity.id:
            raise ProtocolError("Choose the other computer")
        if self.identity.known(der):
            return
        if not interactive or not self.approve(certificate_name(der), address):
            raise ProtocolError("Pairing declined")
        self.identity.remember(der, address)

    def connect(self, address, port=PORT, expected_id=None, interactive=True):
        if not lan_address(address):
            self.on_status("Enter the other computer's local IPv4 address")
            return
        self.paused = False
        threading.Thread(
            target=self._connect, args=(address, port, expected_id, interactive), daemon=True
        ).start()

    def _connect(self, address, port, expected_id, interactive):
        if not self.connecting.acquire(blocking=False):
            return
        sock = None
        try:
            if self.stopped.is_set() or self.session:
                return
            if interactive:
                self.on_status(f"Connecting to {address}…")
            raw = socket.create_connection((address, port), timeout=5)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            # Self-signed certificates: authenticate by an approved exact certificate pin,
            # rather than a public CA. No input or clipboard is sent before authentication.
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                sock = context.wrap_socket(raw, server_hostname="lanmouse")
            except Exception:
                raw.close()
                raise
            sock.settimeout(90)
            der = sock.getpeercert(binary_form=True)
            peer_id = fingerprint(der)
            if expected_id and expected_id != peer_id:
                raise ProtocolError("Computer identity changed; reconnect and approve it again")
            greeting = receive(sock, limit=16384)
            if greeting.get("type") != "challenge" or greeting.get("version") != VERSION:
                raise ProtocolError("Different app version")
            nonce = base64.b64decode(greeting.get("nonce", ""), validate=True)
            if len(nonce) != 32:
                raise ProtocolError("Invalid authentication challenge")
            self._trust(der, address, interactive)
            proof = b"LAN-MOUSE-v1\0" + nonce + bytes.fromhex(peer_id)
            sock.sendall(
                encode(
                    {
                        "type": "identity",
                        "cert": base64.b64encode(self.identity.der).decode(),
                        "proof": self.identity.sign(proof),
                    }
                )
            )
            if receive(sock, limit=16384).get("type") != "welcome":
                raise ProtocolError("Other computer did not approve the connection")
            self.identity.remember(der, address, port)
            self._attach(sock, peer_id, certificate_name(der), address, True)
            sock = None
        except Exception as exc:
            if interactive:
                self.on_status(f"Could not connect: {exc}")
        finally:
            if sock:
                sock.close()
            self.connecting.release()

    def _listen(self):
        while not self.stopped.is_set():
            try:
                sock, (address, _) = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if (
                not lan_address(address)
                or self.paused
                or self.session
                or not self.pending.acquire(blocking=False)
            ):
                sock.close()
                continue
            threading.Thread(target=self._accept, args=(sock, address), daemon=True).start()

    def _accept(self, raw, address):
        sock = None
        try:
            raw.settimeout(10)
            sock = self.context.wrap_socket(raw, server_side=True)
            sock.settimeout(90)
            nonce = secrets.token_bytes(32)
            sock.sendall(
                encode(
                    {
                        "type": "challenge",
                        "version": VERSION,
                        "nonce": base64.b64encode(nonce).decode(),
                    }
                )
            )
            hello = receive(sock, limit=16384)
            if hello["type"] != "identity":
                raise ProtocolError("Missing identity")
            der = base64.b64decode(hello.get("cert", ""), validate=True)
            verify_proof(
                der,
                hello.get("proof", ""),
                b"LAN-MOUSE-v1\0" + nonce + bytes.fromhex(self.identity.id),
            )
            self._trust(der, address, True)
            sock.sendall(encode({"type": "welcome"}))
            self._attach(sock, fingerprint(der), certificate_name(der), address, False)
            sock = None
        except Exception as exc:
            self.on_status(f"Incoming connection: {exc}")
        finally:
            if sock:
                sock.close()
            raw.close()
            self.pending.release()

    def _attach(self, sock, peer_id, name, address, initiator):
        with self.lock:
            if self.session or self.stopped.is_set() or self.paused:
                raise ProtocolError("Already connected or paused")
            session = Session(
                sock, peer_id, name, address, initiator, self.on_message, self._closed
            )
            self.session = session
        self.on_ready(session)
        # The GUI starts reader/writer threads after it installs the session state.

    def _closed(self, session, reason):
        with self.lock:
            if self.session is session:
                self.session = None
        self.on_close(session, reason)

    def _reconnect(self):
        while not self.stopped.wait(3):
            if self.session or self.paused:
                continue
            with self.lock, self.identity.lock:
                candidates = list(self.peers.values())
                known = dict(self.identity.settings["peers"])
            discovered = {p["id"] for p in candidates}
            for peer_id, peer in known.items():
                if peer_id not in discovered:
                    candidates.append(
                        {"id": peer_id, "address": peer["address"], "port": peer.get("port", PORT)}
                    )
            # Only the lower identity initiates automatic reconnect, avoiding double sessions.
            for candidate in candidates:
                peer_id = candidate["id"]
                if peer_id in known and self.identity.id < peer_id:
                    self.connect(candidate["address"], candidate["port"], peer_id, False)
                    break

    def disconnect(self):
        self.paused = True
        if self.session:
            self.session.close("Disconnected; click Connect to resume")

    def stop(self):
        self.stopped.set()
        self.disconnect()
        if self.listener:
            self.listener.close()
        if self.discovery_thread:
            self.discovery_thread.join(timeout=5)
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            if self.info:
                try:
                    self.zeroconf.unregister_service(self.info)
                except Exception:
                    pass
            self.zeroconf.close()
