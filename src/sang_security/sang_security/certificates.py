"""Certificate inspection shared by OPC UA and HTTPS identity checks."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization


class CertificateError(ValueError):
    """Certificate content does not satisfy the SANG identity contract."""


def load_certificate(value: str | Path | bytes) -> x509.Certificate:
    data = value if isinstance(value, bytes) else Path(value).read_bytes()
    try:
        if b"-----BEGIN CERTIFICATE-----" in data:
            return x509.load_pem_x509_certificate(data)
        return x509.load_der_x509_certificate(data)
    except ValueError as exc:
        raise CertificateError("certificate is neither valid PEM nor DER") from exc


def certificate_sha256(value: str | Path | bytes | x509.Certificate) -> str:
    certificate = value if isinstance(value, x509.Certificate) else load_certificate(value)
    encoded = certificate.public_bytes(serialization.Encoding.DER)
    return sha256(encoded).hexdigest()


def certificate_uri_sans(certificate: x509.Certificate) -> tuple[str, ...]:
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except x509.ExtensionNotFound as exc:
        raise CertificateError("certificate is missing SubjectAlternativeName") from exc
    return tuple(
        extension.value.get_values_for_type(x509.UniformResourceIdentifier)
    )


def require_identity_uri(certificate: x509.Certificate, expected: str) -> None:
    if expected not in certificate_uri_sans(certificate):
        raise CertificateError(f"certificate does not contain identity URI {expected!r}")
