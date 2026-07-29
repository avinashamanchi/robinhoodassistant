"""Inspect local-only TLS material without ever printing private key bytes."""

from __future__ import annotations

import argparse
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed25519,
    ed448,
    padding,
    rsa,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from ..config import load_config


class TLSMaterialError(RuntimeError):
    """A stable, secret-free TLS material inspection failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TLSMaterialStatus:
    ca_certificate_path: Path
    certificate_path: Path
    private_key_path: Path
    sans: tuple[str, ...]


def _tls_directory() -> Path:
    """Return the fixed repository TLS root without resolving attacker input."""
    repository = Path.cwd().resolve()
    local_root = repository / ".local"
    tls_root = local_root / "tls"
    if local_root.is_symlink() or tls_root.is_symlink():
        raise TLSMaterialError("tls_root_symlink_forbidden")
    try:
        canonical_local = local_root.resolve(strict=True)
        canonical_tls = tls_root.resolve(strict=True)
    except OSError:
        raise TLSMaterialError("tls_directory_permissions_invalid") from None
    if (
        canonical_local != local_root
        or canonical_tls != tls_root
        or not canonical_tls.is_dir()
    ):
        raise TLSMaterialError("tls_root_symlink_forbidden")
    return canonical_tls


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise TLSMaterialError("tls_path_outside_local_directory") from None
    return resolved


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _signature_is_valid(
    certificate: x509.Certificate,
    issuer: x509.Certificate,
) -> bool:
    public_key = issuer.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                certificate.signature_hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(certificate.signature_hash_algorithm),
            )
        elif isinstance(public_key, dsa.DSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                certificate.signature_hash_algorithm,
            )
        elif isinstance(
            public_key,
            (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey),
        ):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
            )
        else:
            return False
    except Exception:
        return False
    return True


def validate_tls_material(server) -> TLSMaterialStatus:
    """Validate local certificate/key material required by the strict launcher."""
    root = _tls_directory()
    ca_certificate_path = _contained(Path(server.tls_ca_path), root)
    certificate_path = _contained(Path(server.tls_cert_path), root)
    private_key_path = _contained(Path(server.tls_key_path), root)
    if _mode(root) != 0o700:
        raise TLSMaterialError("tls_directory_permissions_invalid")
    if (
        not ca_certificate_path.is_file()
        or _mode(ca_certificate_path) != 0o644
    ):
        raise TLSMaterialError("tls_ca_permissions_invalid")
    if not certificate_path.is_file() or _mode(certificate_path) != 0o644:
        raise TLSMaterialError("tls_certificate_permissions_invalid")
    if not private_key_path.is_file() or _mode(private_key_path) != 0o600:
        raise TLSMaterialError("tls_private_key_permissions_invalid")

    try:
        ca_certificate = x509.load_pem_x509_certificate(
            ca_certificate_path.read_bytes()
        )
        ca_constraints = ca_certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
    except Exception:
        raise TLSMaterialError("tls_ca_invalid") from None
    now = datetime.now(timezone.utc)
    if (
        not ca_constraints.ca
        or ca_certificate.subject != ca_certificate.issuer
        or not (
            ca_certificate.not_valid_before_utc
            <= now
            <= ca_certificate.not_valid_after_utc
        )
        or not _signature_is_valid(ca_certificate, ca_certificate)
    ):
        raise TLSMaterialError("tls_ca_invalid")

    try:
        certificate = x509.load_pem_x509_certificate(
            certificate_path.read_bytes()
        )
        private_key = load_pem_private_key(
            private_key_path.read_bytes(),
            password=None,
        )
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except Exception:
        raise TLSMaterialError("tls_material_parse_failed") from None

    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise TLSMaterialError("tls_certificate_not_current")
    if (
        certificate.issuer != ca_certificate.subject
        or not _signature_is_valid(certificate, ca_certificate)
    ):
        raise TLSMaterialError("tls_ca_chain_invalid")
    names = extension.value
    dns_names = {value.lower() for value in names.get_values_for_type(x509.DNSName)}
    ip_names = {
        str(value) for value in names.get_values_for_type(x509.IPAddress)
    }
    if dns_names != {"localhost"} or ip_names != {"127.0.0.1", "::1"}:
        raise TLSMaterialError("tls_certificate_san_invalid")
    certificate_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_public != key_public:
        raise TLSMaterialError("tls_certificate_key_mismatch")
    return TLSMaterialStatus(
        ca_certificate_path=ca_certificate_path,
        certificate_path=certificate_path,
        private_key_path=private_key_path,
        sans=("localhost", "127.0.0.1", "::1"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect",))
    args = parser.parse_args(argv)
    if args.command == "inspect":
        try:
            status = validate_tls_material(load_config().server)
        except TLSMaterialError as exc:
            print(f"TLS inspection failed: {exc.code}")
            return 1
        print(
            "TLS inspection passed: "
            f"certificate={status.certificate_path.name} "
            f"sans={','.join(status.sans)}"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
