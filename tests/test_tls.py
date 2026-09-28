"""The built in https certificate, made for real under tmp_path."""

import ipaddress
import socket
import ssl
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from app import tls


def _cert(path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _names(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (
        set(alt.get_values_for_type(x509.DNSName)),
        {str(address) for address in alt.get_values_for_type(x509.IPAddress)},
    )


def test_certificate_is_made_in_the_directory(tmp_path):
    directory = tmp_path / "not" / "there" / "yet"

    cert_path, key_path, fingerprint = tls.ensure_certificate(directory)

    assert cert_path == directory / "cert.pem"
    assert key_path == directory / "key.pem"
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert fingerprint == tls.fingerprint(_cert(cert_path))


def test_certificate_is_self_signed_for_a_server(tmp_path):
    before = datetime.now(timezone.utc)
    cert_path, key_path, _ = tls.ensure_certificate(tmp_path)
    cert = _cert(cert_path)

    assert cert.issuer == cert.subject
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    public_key = cert.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert public_key.curve.name == "secp256r1"
    # raises when the certificate was not signed by its own key
    cert.verify_directly_issued_by(cert)

    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False

    assert cert.not_valid_after_utc - cert.not_valid_before_utc == timedelta(days=397)
    assert cert.not_valid_before_utc <= before
    assert before - cert.not_valid_before_utc < timedelta(minutes=10)


def test_key_belongs_to_the_certificate_and_only_the_owner_can_read_it(tmp_path):
    cert_path, key_path, _ = tls.ensure_certificate(tmp_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert key.public_key() == _cert(cert_path).public_key()


def test_key_mode_is_put_right_when_the_file_was_open_to_others(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_text("left over")
    key_path.chmod(0o644)

    tls.ensure_certificate(tmp_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_names_in_the_certificate(tmp_path):
    hosts = ["boxes.example.com", "192.0.2.10", "2001:db8::10", " Garage.Example.COM "]

    cert_path, _, _ = tls.ensure_certificate(tmp_path, hosts)
    dns, addresses = _names(_cert(cert_path))

    assert {"localhost", "boxes.example.com", "garage.example.com"} <= dns
    assert socket.gethostname().lower() in dns
    assert {"127.0.0.1", "192.0.2.10", "2001:db8::10"} <= addresses
    # an address is stored as an address, never as a name, or a browser would not match it
    for name in dns:
        with pytest.raises(ValueError):
            ipaddress.ip_address(name)


def test_this_machines_own_address_is_in_the_certificate(tmp_path):
    address = tls.lan_address()

    cert_path, _, _ = tls.ensure_certificate(tmp_path)
    _, addresses = _names(_cert(cert_path))

    if address:
        assert ipaddress.ip_address(address).version == 4
        assert address in addresses
    else:
        # a machine with no network at all still gets a certificate for itself
        assert addresses == {"127.0.0.1"}


def test_wanted_names_are_clean_and_stable():
    names = tls.wanted_names(["Boxes.Example.com", "boxes.example.com", "", "  ", "localhost", "192.0.2.10"])

    assert names[:2] == ["localhost", "127.0.0.1"]
    assert names.count("localhost") == 1
    assert names.count("boxes.example.com") == 1
    assert "" not in names
    assert names[-2:] == ["boxes.example.com", "192.0.2.10"]
    assert tls.wanted_names(["boxes.example.com", "192.0.2.10"]) == names


def test_second_call_reuses_the_certificate(tmp_path):
    first = tls.ensure_certificate(tmp_path, ["boxes.example.com"])
    cert_bytes, key_bytes = first[0].read_bytes(), first[1].read_bytes()

    second = tls.ensure_certificate(tmp_path, ["boxes.example.com"])
    # asking for fewer names than it holds is no reason to make a new one
    third = tls.ensure_certificate(tmp_path)

    assert second == first
    assert third == first
    assert first[0].read_bytes() == cert_bytes
    assert first[1].read_bytes() == key_bytes


def test_a_new_host_makes_a_new_certificate(tmp_path):
    cert_path, key_path, first = tls.ensure_certificate(tmp_path, ["boxes.example.com"])
    key_bytes = key_path.read_bytes()

    _, _, second = tls.ensure_certificate(tmp_path, ["boxes.example.com", "garage.example.com", "192.0.2.20"])

    assert second != first
    assert key_path.read_bytes() != key_bytes
    dns, addresses = _names(_cert(cert_path))
    assert {"boxes.example.com", "garage.example.com"} <= dns
    assert "192.0.2.20" in addresses
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_certificate_is_renewed_within_30_days_of_expiry(tmp_path):
    made = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _, _, first = tls.ensure_certificate(tmp_path, now=made)

    # 397 days of validity, so day 366 leaves 31 and day 368 leaves 29
    still_good = tls.ensure_certificate(tmp_path, now=made + timedelta(days=366))[2]
    assert still_good == first

    renewed_at = made + timedelta(days=368)
    cert_path, _, renewed = tls.ensure_certificate(tmp_path, now=renewed_at)
    assert renewed != first
    assert _cert(cert_path).not_valid_after_utc - renewed_at > timedelta(days=396)


def test_a_damaged_or_mismatched_pair_is_replaced(tmp_path):
    cert_path, key_path, first = tls.ensure_certificate(tmp_path)

    cert_path.write_text("not a certificate")
    _, _, second = tls.ensure_certificate(tmp_path)
    assert second != first

    other = tmp_path / "other"
    _, other_key, _ = tls.ensure_certificate(other)
    key_path.write_bytes(other_key.read_bytes())
    _, _, third = tls.ensure_certificate(tmp_path)
    assert third != second

    key_path.unlink()
    _, _, fourth = tls.ensure_certificate(tmp_path)
    assert fourth != third
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert key.public_key() == _cert(cert_path).public_key()


def test_fingerprint_is_the_sha256_a_browser_shows(tmp_path):
    cert_path, _, fingerprint = tls.ensure_certificate(tmp_path)

    der = _cert(cert_path).public_bytes(serialization.Encoding.DER)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    expected = digest.finalize().hex().upper()

    parts = fingerprint.split(":")
    assert len(parts) == 32
    assert "".join(parts) == expected


def test_the_pair_loads_into_a_tls_server_context(tmp_path):
    cert_path, key_path, _ = tls.ensure_certificate(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    # raises when the certificate and key do not belong together
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)


def test_finding_the_address_sends_nothing():
    # 192.0.2.0/24 is set aside for documentation and is never routed
    assert ipaddress.ip_address(tls.PROBE_ADDRESS[0]) in ipaddress.ip_network("192.0.2.0/24")
    address = tls.lan_address()
    assert address == "" or ipaddress.ip_address(address).version == 4
