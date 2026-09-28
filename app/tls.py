"""Optional built in https: a self signed certificate made on this machine.

Phone browsers only open the camera on an https page. This lets a person at
home turn https on without running a proxy. The certificate is made once and
kept, so the phone asks about it once. It is made again only when it is about
to expire or when a name it should cover is missing from it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("scannage.tls")

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
VALID_DAYS = 397
RENEW_DAYS = 30
COMMON_NAME = "Scannage"

# A documentation address (RFC 5737). Connecting a UDP socket to it sends
# nothing; it only makes the system pick the interface it would route through.
PROBE_ADDRESS = ("192.0.2.1", 9)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def lan_address() -> str:
    """This machine's address on the local network, or empty when it has none."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(PROBE_ADDRESS)
            address = probe.getsockname()[0]
    except OSError:
        return ""
    return "" if address.startswith("0.") else address


def wanted_names(extra: Iterable[str] = ()) -> list[str]:
    """Everything the certificate should answer to, in a stable order without repeats."""
    names = ["localhost", "127.0.0.1", socket.gethostname(), lan_address(), *extra]
    wanted: list[str] = []
    for name in names:
        name = name.strip().lower()
        if name and name.isascii() and name not in wanted:
            wanted.append(name)
    return wanted


def _as_address(name: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def _covered_names(cert: x509.Certificate) -> set[str]:
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    covered = {name.lower() for name in names.get_values_for_type(x509.DNSName)}
    return covered | {str(address) for address in names.get_values_for_type(x509.IPAddress)}


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 of the certificate, written the way a browser shows it."""
    return ":".join(f"{byte:02X}" for byte in cert.fingerprint(hashes.SHA256()))


def _usable(cert_path: Path, key_path: Path, names: list[str], now: datetime) -> x509.Certificate | None:
    """The certificate on disk when it can go on being served, otherwise None."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if key.public_key() != cert.public_key():
        log.info("the key does not belong to the certificate")
        return None
    if cert.not_valid_after_utc - now < timedelta(days=RENEW_DAYS):
        log.info("the certificate expires on %s", cert.not_valid_after_utc.date())
        return None
    missing = [name for name in names if str(_as_address(name) or name) not in _covered_names(cert)]
    if missing:
        log.info("the certificate does not cover %s", ", ".join(missing))
        return None
    return cert


def _generate(cert_path: Path, key_path: Path, names: list[str], now: datetime) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    alt_names: list[x509.GeneralName] = []
    for name in names:
        address = _as_address(name)
        alt_names.append(x509.IPAddress(address) if address else x509.DNSName(name))
    usage = x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    # backdated a little, so a phone whose clock runs behind still accepts it
    starts = now - timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(starts)
        .not_valid_after(starts + timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    # created as 0600, so the key is never readable by anyone else, not even briefly
    handle = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as key_file:
        os.fchmod(key_file.fileno(), 0o600)
        key_file.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


def ensure_certificate(
    directory: Path | str, extra_hosts: Iterable[str] = (), now: datetime | None = None
) -> tuple[Path, Path, str]:
    """Paths of the certificate and its key, made when needed, and the certificate's fingerprint."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    names = wanted_names(extra_hosts)
    now = now or datetime.now(timezone.utc)
    cert = _usable(cert_path, key_path, names, now)
    if cert is None:
        cert = _generate(cert_path, key_path, names, now)
        log.info("made a self signed certificate for %s", ", ".join(names))
    return cert_path, key_path, fingerprint(cert)
