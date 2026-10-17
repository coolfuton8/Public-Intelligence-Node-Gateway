"""Local certificate authority and server certificate for the device's HTTPS endpoint.

One long-lived CA (name-constrained to .local, localhost and mcping names, so a stolen key
cannot be used to impersonate public websites) signs a short-lived server certificate. Clients
trust the CA once; renewing the server certificate never needs them to do anything.

    mcping-cert status | renew | export <file>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_DIR = Path(os.environ.get("MCPING_TLS_DIR", "/var/lib/mcping/tls"))
CA_DAYS = 3650
LEAF_DAYS = 365
RENEW_BEFORE_DAYS = 30

_PERMITTED_DNS = ("local", "localhost", "mcping")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def paths(d: Path | None = None) -> dict[str, Path]:
    d = Path(d or DEFAULT_DIR)
    return {"dir": d, "ca_key": d / "ca.key", "ca_crt": d / "ca.crt",
            "key": d / "server.key", "crt": d / "server.crt"}


def local_ips() -> list[str]:
    """Global IPv4 addresses of this machine."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    except (OSError, subprocess.TimeoutExpired):
        ips = []
    return sorted(set(ips))


def _permitted(name: str) -> bool:
    n = name.lower()
    return any(n == p or n.endswith("." + p) for p in _PERMITTED_DNS)


def wanted_names() -> list[str]:
    host = socket.gethostname().split(".")[0]
    names = ["mcping.local", "mcping", "localhost", host, f"{host}.local"]
    out: list[str] = []
    for n in names:
        if n and _permitted(n) and n not in out:
            out.append(n)
    return out


def _write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ensure_ca(d: Path | None = None):
    p = paths(d)
    p["dir"].mkdir(parents=True, exist_ok=True)
    if p["ca_key"].exists() and p["ca_crt"].exists():
        return (x509.load_pem_x509_certificate(p["ca_crt"].read_bytes()),
                serialization.load_pem_private_key(p["ca_key"].read_bytes(), None))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCPing device CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MCPing")])
    # DNS names only: Claude Code's TLS stack rejects certificates whose CA carries an IP-range
    # name constraint (UNSUPPORTED_CONSTRAINT_TYPE), so that restriction is left out.
    constraints = x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(n) for n in _PERMITTED_DNS], excluded_subtrees=None)
    now = _now()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(constraints, critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write(p["ca_key"], _key_pem(key), 0o600)
    _write(p["ca_crt"], cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert, key


def issue_server(d: Path | None = None) -> x509.Certificate:
    """Create a fresh server key and certificate for this machine's current names and IPs."""
    p = paths(d)
    ca_cert, ca_key = ensure_ca(d)
    key = ec.generate_private_key(ec.SECP256R1())
    names = wanted_names()
    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    for ip in ["127.0.0.1"] + local_ips():
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    now = _now()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    # key first, certificate last: the service restarts when the certificate file changes
    _write(p["key"], _key_pem(key), 0o600)
    _write(p["crt"], cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert


def _load(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def _sans(cert) -> tuple[list[str], list[str]]:
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return ext.get_values_for_type(x509.DNSName), [str(i) for i in ext.get_values_for_type(x509.IPAddress)]


def renewal_reason(d: Path | None = None) -> str | None:
    """Why the server certificate should be reissued now, or None if it is fine."""
    p = paths(d)
    if not (p["crt"].exists() and p["key"].exists() and p["ca_crt"].exists()):
        return "no certificate yet"
    cert = _load(p["crt"])
    left = (cert.not_valid_after_utc - _now()).days
    if left < RENEW_BEFORE_DAYS:
        return f"expires in {left} days" if left >= 0 else "expired"
    dns, ips = _sans(cert)
    missing = [i for i in local_ips() if i not in ips]
    if missing:
        return "device address changed (" + ", ".join(missing) + ")"
    return None


def ensure(d: Path | None = None, force: bool = False) -> str:
    """Create or renew as needed. Returns a short description of what was done."""
    why = "requested" if force else renewal_reason(d)
    if why is None:
        return "certificate is valid; nothing to do"
    issue_server(d)
    return f"certificate issued ({why})"


def info(d: Path | None = None) -> dict:
    p = paths(d)
    out: dict = {"dir": str(p["dir"]), "ok": False}
    if not p["crt"].exists():
        out["problem"] = "no certificate"
        return out
    cert = _load(p["crt"])
    ca = _load(p["ca_crt"])
    dns, ips = _sans(cert)
    left = (cert.not_valid_after_utc - _now()).days
    out.update(ok=left >= 0, names=dns, addresses=ips, expires=cert.not_valid_after_utc.date().isoformat(),
               days_left=left, serial=format(cert.serial_number, "x")[:12],
               ca_expires=ca.not_valid_after_utc.date().isoformat(),
               ca_fingerprint=hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest(),
               renewal=renewal_reason(d))
    return out


def export_ca(dest: str | Path, d: Path | None = None) -> Path:
    p = paths(d)
    ensure_ca(d)
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / "mcping-ca.crt"
    dest.write_bytes(p["ca_crt"].read_bytes())
    return dest


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "status"
    if cmd == "status":
        print(json.dumps(info(), indent=2))
    elif cmd == "renew":
        print(ensure(force=True))
        print(json.dumps(info(), indent=2))
    elif cmd == "ensure":
        print(ensure())
    elif cmd == "export" and len(a) > 1:
        print(export_ca(a[1]))
    else:
        print("usage: mcping-cert status | renew | ensure | export <file-or-folder>")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
