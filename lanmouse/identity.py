"""Local TLS identity and certificate pinning after an explicit pairing approval."""

import base64
import datetime as dt
import hashlib
import json
import os
import socket
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from .core import PORT, ProtocolError


def state_directory():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "lan-mouse-python"


def fingerprint(der):
    return hashlib.sha256(der).hexdigest()


def certificate_name(der):
    cert = x509.load_der_x509_certificate(der)
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return "".join(c for c in (names[0].value if names else "Computer") if c.isprintable())[:80]


class Identity:
    def __init__(self, directory=None, name=None):
        self.directory = Path(directory) if directory else state_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.key_path = self.directory / "identity.key"
        self.cert_path = self.directory / "identity.pem"
        self.settings_path = self.directory / "settings.json"
        if not self.key_path.exists() or not self.cert_path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, (name or socket.gethostname())[:63])]
            )
            now = dt.datetime.now(dt.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650))
                .sign(key, hashes.SHA256())
            )
            self._write_private(
                self.key_path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            self._write_private(self.cert_path, cert.public_bytes(serialization.Encoding.PEM))
        self.key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        self.der = x509.load_pem_x509_certificate(self.cert_path.read_bytes()).public_bytes(
            serialization.Encoding.DER
        )
        self.id = fingerprint(self.der)
        self.name = certificate_name(self.der)
        try:
            self.settings = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            self.settings = {}
        self.settings.setdefault("peers", {})

    @staticmethod
    def _write_private(path, content):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(content)
        if os.name != "nt":
            path.chmod(0o600)

    def save(self):
        with self.lock:
            temporary = self.settings_path.with_suffix(".tmp")
            self._write_private(temporary, json.dumps(self.settings, indent=2).encode())
            temporary.replace(self.settings_path)

    def known(self, der):
        with self.lock:
            return fingerprint(der) in self.settings["peers"]

    def remember(self, der, address, port=PORT):
        with self.lock:
            self.settings["peers"][fingerprint(der)] = {
                "name": certificate_name(der),
                "address": address,
                "port": port,
            }
            self.save()

    def mark_connected(self, peer_id):
        with self.lock:
            if peer_id in self.settings["peers"] and self.settings.get("last_peer") != peer_id:
                self.settings["last_peer"] = peer_id
                self.save()

    def sign(self, challenge):
        return base64.b64encode(
            self.key.sign(
                challenge,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
        ).decode()

    def server_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_path, self.key_path)
        return context


def verify_proof(der, signature, challenge):
    try:
        cert = x509.load_der_x509_certificate(der)
        key = cert.public_key()
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ProtocolError("Unsupported identity")
        key.verify(
            base64.b64decode(signature, validate=True),
            challenge,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise ProtocolError("Computer identity verification failed") from exc
