"""Small lab PKI with separate OPC UA and HTTPS issuing authorities."""

from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from sang_security.certificates import (
    CertificateError,
    certificate_sha256,
    load_certificate,
    require_identity_uri,
)


UTC = timezone.utc
STATE_VERSION = 1
CRL_VALIDITY_DAYS = 30
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ISSUER_ROLES = {
    "opcua": {"opcua-server", "opcua-client", "opcua-user"},
    "https": {"https-server", "https-client"},
}


class PkiError(ValueError):
    """The requested PKI operation is invalid or would overwrite key material."""


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    _write_atomic(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode,
    )


def _private_key_bytes(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _certificate_bytes(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _load_private_key(path: Path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise PkiError(f"cannot load private key: {path}") from exc


def _ca_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=None,
        decipher_only=None,
    )


def _leaf_key_usage() -> x509.KeyUsage:
    # asyncua's strict validator requires all four application usages.
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=True,
        key_encipherment=True,
        data_encipherment=True,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=None,
        decipher_only=None,
    )


def _ca_certificate(
    *,
    subject: x509.Name,
    public_key,
    issuer: x509.Name,
    signing_key,
    now: datetime,
    days: int,
    path_length: int,
) -> x509.Certificate:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), True)
        .add_extension(_ca_key_usage(), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), False)
        .sign(signing_key, hashes.SHA256())
    )


def _issuer_paths(root: Path, issuer: str) -> tuple[Path, Path]:
    base = root / "issuers" / issuer
    return base / "ca-cert.pem", base / "ca-key.pem"


def _load_state(root: Path) -> dict[str, Any]:
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PkiError("PKI state is missing or invalid") from exc
    if state.get("schema_version") != STATE_VERSION:
        raise PkiError("unsupported PKI state schema")
    return state


def _issuance_metadata(
    certificate: x509.Certificate,
    *,
    name: str,
    issuer: str,
    role: str,
    uri: str | None,
    dns_names: tuple[str, ...],
    ip_addresses: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "issuer": issuer,
        "role": role,
        "uri": uri,
        "dns_names": list(dns_names),
        "ip_addresses": list(ip_addresses),
        "serial_number": str(certificate.serial_number),
        "certificate_sha256": certificate_sha256(certificate),
        "not_before": certificate.not_valid_before_utc.isoformat(),
        "not_after": certificate.not_valid_after_utc.isoformat(),
        "status": "active",
    }


def _recover_existing_issuance(
    output: Path,
    *,
    target: Path,
    state: dict[str, Any],
    issuer: str,
    issuer_certificate: x509.Certificate,
    name: str,
    role: str,
    uri: str | None,
    dns_names: tuple[str, ...],
    ip_addresses: tuple[str, ...],
) -> dict[str, Any]:
    key_path = output / "key.pem"
    cert_path = output / "cert.pem"
    chain_path = output / "chain.pem"
    if not key_path.is_file() or not cert_path.is_file():
        raise PkiError(f"incomplete certificate output requires operator quarantine: {output}")
    certificate = load_certificate(cert_path)
    key = _load_private_key(key_path)
    if certificate.issuer != issuer_certificate.subject:
        raise PkiError("existing certificate was signed by a different issuer")
    try:
        certificate.verify_directly_issued_by(issuer_certificate)
    except ValueError as exc:
        raise PkiError("existing certificate issuer signature is invalid") from exc
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1 or common_names[0].value != name:
        raise PkiError("existing certificate subject differs from the requested name")
    if key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ):
        raise PkiError("existing certificate and private key do not match")
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    actual_uris = tuple(san.get_values_for_type(x509.UniformResourceIdentifier))
    actual_dns = tuple(san.get_values_for_type(x509.DNSName))
    actual_ips = tuple(str(value) for value in san.get_values_for_type(x509.IPAddress))
    expected_uris = () if uri is None else (uri,)
    expected_ips = tuple(str(ipaddress.ip_address(value)) for value in ip_addresses)
    if actual_uris != expected_uris or actual_dns != dns_names or actual_ips != expected_ips:
        raise PkiError("existing certificate SANs differ from the requested identity")
    expected_eku = (
        ExtendedKeyUsageOID.SERVER_AUTH
        if role.endswith("server")
        else ExtendedKeyUsageOID.CLIENT_AUTH
    )
    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if expected_eku not in eku:
        raise PkiError("existing certificate role differs from the requested role")
    metadata = _issuance_metadata(
        certificate,
        name=name,
        issuer=issuer,
        role=role,
        uri=uri,
        dns_names=dns_names,
        ip_addresses=ip_addresses,
    )
    existing = state["issued"].get(metadata["serial_number"])
    if existing is not None and existing.get("status") != "active":
        raise PkiError("revoked certificate output cannot be reused")
    _write_atomic(
        chain_path,
        _certificate_bytes(certificate) + _certificate_bytes(issuer_certificate),
        0o644,
    )
    _write_json(output / "metadata.json", metadata, 0o644)
    state["issued"][metadata["serial_number"]] = metadata
    _write_json(target / "state.json", state)
    return {
        **metadata,
        "certificate": str(cert_path),
        "private_key": str(key_path),
        "chain": str(chain_path),
    }


def _refresh_trust_material(root: Path, issuer: str, state: dict[str, Any]) -> None:
    issuer_cert_path, issuer_key_path = _issuer_paths(root, issuer)
    issuer_cert = load_certificate(issuer_cert_path)
    issuer_key = _load_private_key(issuer_key_path)
    revoked_entries = state["issuers"][issuer]["revoked"]
    now = datetime.now(UTC)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer_cert.subject)
        .last_update(now - timedelta(minutes=1))
        .next_update(now + timedelta(days=CRL_VALIDITY_DAYS))
    )
    for serial, metadata in sorted(revoked_entries.items()):
        revoked = (
            x509.RevokedCertificateBuilder()
            .serial_number(int(serial))
            .revocation_date(datetime.fromisoformat(metadata["revoked_at"]))
            .add_extension(x509.CRLReason(x509.ReasonFlags.unspecified), False)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked)
    crl = builder.sign(issuer_key, hashes.SHA256())

    trust_dir = root / "trust" / issuer
    crl_dir = root / "crl" / issuer
    (trust_dir / "root-ca.pem").unlink(missing_ok=True)
    _write_atomic(trust_dir / f"{issuer}-ca.pem", _certificate_bytes(issuer_cert), 0o644)
    _write_atomic(crl_dir / f"{issuer}-crl.pem", crl.public_bytes(serialization.Encoding.PEM), 0o644)
    ca_bundle = _certificate_bytes(issuer_cert)
    _write_atomic(root / "bundles" / f"{issuer}-ca.pem", ca_bundle, 0o644)
    _write_atomic(
        root / "bundles" / f"{issuer}-ca-crl.pem",
        ca_bundle + crl.public_bytes(serialization.Encoding.PEM),
        0o644,
    )
    roles = root / "roles"
    if issuer == "https":
        destinations = [
            roles / role / "https/trust/ca-crl.pem"
            for role in ("central", "edge")
        ]
        agents = roles / "agents"
        if agents.is_dir():
            destinations.extend(
                role / "edge/https/trust/ca-crl.pem"
                for role in agents.iterdir()
                if role.is_dir()
            )
        for destination in destinations:
            if destination.parent.exists():
                _write_atomic(
                    destination,
                    ca_bundle + crl.public_bytes(serialization.Encoding.PEM),
                    0o644,
                )
    else:
        for role in ("central", "edge"):
            destination = roles / role / "opcua/crl/opcua-crl.pem"
            if destination.parent.exists():
                _write_atomic(
                    destination,
                    crl.public_bytes(serialization.Encoding.PEM),
                    0o644,
                )
        agents = roles / "agents"
        if agents.is_dir():
            for role in agents.iterdir():
                destination = role / "edge/opcua/crl/opcua-crl.pem"
                if destination.parent.exists():
                    _write_atomic(
                        destination,
                        crl.public_bytes(serialization.Encoding.PEM),
                        0o644,
                    )


def initialize_pki(
    root: str | Path,
    *,
    common_name: str = "SANG Lab",
    now: datetime | None = None,
    key_size: int = 3072,
) -> dict[str, Any]:
    target = Path(root).expanduser()
    if target.exists() and any(target.iterdir()):
        raise PkiError("PKI directory must be absent or empty")
    if key_size < 2048:
        raise PkiError("RSA keys must be at least 2048 bits")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    issued_at = now or datetime.now(UTC)

    root_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{common_name} Root CA")])
    root_cert = _ca_certificate(
        subject=root_name,
        public_key=root_key.public_key(),
        issuer=root_name,
        signing_key=root_key,
        now=issued_at,
        days=3650,
        path_length=1,
    )
    _write_atomic(target / "root" / "ca-key.pem", _private_key_bytes(root_key), 0o600)
    _write_atomic(target / "root" / "ca-cert.pem", _certificate_bytes(root_cert), 0o644)

    state = {
        "schema_version": STATE_VERSION,
        "common_name": common_name,
        "created_at": issued_at.isoformat(),
        "key_size": key_size,
        "issuers": {},
        "issued": {},
    }
    for issuer in ISSUER_ROLES:
        key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, f"{common_name} {issuer.upper()} Issuing CA")]
        )
        certificate = _ca_certificate(
            subject=name,
            public_key=key.public_key(),
            issuer=root_cert.subject,
            signing_key=root_key,
            now=issued_at,
            days=1825,
            path_length=0,
        )
        cert_path, key_path = _issuer_paths(target, issuer)
        _write_atomic(key_path, _private_key_bytes(key), 0o600)
        _write_atomic(cert_path, _certificate_bytes(certificate), 0o644)
        state["issuers"][issuer] = {"revoked": {}}

    _write_json(target / "state.json", state)
    for issuer in ISSUER_ROLES:
        _refresh_trust_material(target, issuer, state)
    return {
        "root": str(target.resolve()),
        "root_certificate_sha256": certificate_sha256(root_cert),
        "issuers": sorted(ISSUER_ROLES),
    }


def issue_certificate(
    root: str | Path,
    *,
    issuer: str,
    name: str,
    role: str,
    uri: str | None = None,
    dns_names: tuple[str, ...] = (),
    ip_addresses: tuple[str, ...] = (),
    days: int = 365,
    now: datetime | None = None,
) -> dict[str, Any]:
    target = Path(root).expanduser().resolve(strict=True)
    if issuer not in ISSUER_ROLES or role not in ISSUER_ROLES[issuer]:
        raise PkiError("certificate role does not match the selected issuer")
    if not SAFE_NAME.fullmatch(name):
        raise PkiError("certificate name contains unsupported characters")
    if days <= 0 or days > 825:
        raise PkiError("leaf certificate validity must be between 1 and 825 days")
    if role.startswith("opcua-") and not uri:
        raise PkiError("OPC UA certificates require an application or user URI")
    if role == "https-client" and not uri:
        raise PkiError("HTTPS client certificates require an Agent identity URI")
    if role == "https-server" and not dns_names and not ip_addresses:
        raise PkiError("HTTPS server certificates require a DNS name or IP address")

    state = _load_state(target)
    issuer_cert_path, issuer_key_path = _issuer_paths(target, issuer)
    issuer_cert = load_certificate(issuer_cert_path)
    issuer_key = _load_private_key(issuer_key_path)
    output = target / "issued" / issuer / name
    if output.exists():
        return _recover_existing_issuance(
            output,
            target=target,
            state=state,
            issuer=issuer,
            issuer_certificate=issuer_cert,
            name=name,
            role=role,
            uri=uri,
            dns_names=dns_names,
            ip_addresses=ip_addresses,
        )
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=int(state["key_size"]),
    )
    issued_at = now or datetime.now(UTC)
    not_before = issued_at - timedelta(minutes=5)
    not_after = issued_at + timedelta(days=days)
    san_values: list[x509.GeneralName] = []
    if uri is not None:
        san_values.append(x509.UniformResourceIdentifier(uri))
    san_values.extend(x509.DNSName(value) for value in dns_names)
    try:
        san_values.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses)
    except ValueError as exc:
        raise PkiError("invalid certificate IP address") from exc
    eku = (
        ExtendedKeyUsageOID.SERVER_AUTH
        if role.endswith("server")
        else ExtendedKeyUsageOID.CLIENT_AUTH
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(_leaf_key_usage(), True)
        .add_extension(x509.ExtendedKeyUsage([eku]), False)
        .add_extension(x509.SubjectAlternativeName(san_values), False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            False,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    metadata = _issuance_metadata(
        certificate,
        name=name,
        issuer=issuer,
        role=role,
        uri=uri,
        dns_names=dns_names,
        ip_addresses=ip_addresses,
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=output.parent))
    try:
        _write_atomic(staging / "key.pem", _private_key_bytes(key), 0o600)
        _write_atomic(staging / "cert.pem", _certificate_bytes(certificate), 0o644)
        _write_atomic(
            staging / "chain.pem",
            _certificate_bytes(certificate) + _certificate_bytes(issuer_cert),
            0o644,
        )
        _write_json(staging / "metadata.json", metadata, 0o644)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    key_path = output / "key.pem"
    cert_path = output / "cert.pem"
    chain_path = output / "chain.pem"
    state["issued"][str(certificate.serial_number)] = metadata
    _write_json(target / "state.json", state)
    return {
        **metadata,
        "certificate": str(cert_path),
        "private_key": str(key_path),
        "chain": str(chain_path),
    }


def revoke_certificate(
    root: str | Path,
    *,
    certificate_path: str | Path,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not reason.strip() or len(reason.strip()) > 512:
        raise PkiError("revocation reason must contain 1 to 512 characters")
    target = Path(root).expanduser().resolve(strict=True)
    certificate = load_certificate(certificate_path)
    state = _load_state(target)
    serial = str(certificate.serial_number)
    metadata = state["issued"].get(serial)
    if metadata is None:
        raise PkiError("certificate was not issued by this PKI")
    issuer = metadata["issuer"]
    existing = state["issuers"][issuer]["revoked"].get(serial)
    if existing is not None:
        _refresh_trust_material(target, issuer, state)
        return {
            "serial_number": serial,
            "certificate_sha256": certificate_sha256(certificate),
            "issuer": issuer,
            "status": "revoked",
            "revoked_at": existing["revoked_at"],
            "reason": existing["reason"],
        }
    revoked_at = now or datetime.now(UTC)
    state["issuers"][issuer]["revoked"][serial] = {
        "revoked_at": revoked_at.isoformat(),
        "reason": reason.strip(),
    }
    metadata["status"] = "revoked"
    metadata["revoked_at"] = revoked_at.isoformat()
    metadata["revocation_reason"] = reason.strip()
    _write_json(target / "state.json", state)
    _refresh_trust_material(target, issuer, state)
    return {
        "serial_number": serial,
        "certificate_sha256": certificate_sha256(certificate),
        "issuer": issuer,
        "status": "revoked",
        "revoked_at": revoked_at.isoformat(),
        "reason": reason.strip(),
    }


def refresh_crls(
    root: str | Path,
    *,
    issuer: str | None = None,
) -> dict[str, Any]:
    target = Path(root).expanduser().resolve(strict=True)
    state = _load_state(target)
    issuers = tuple(ISSUER_ROLES) if issuer is None else (issuer,)
    if any(value not in ISSUER_ROLES for value in issuers):
        raise PkiError("unknown PKI issuer")
    for value in issuers:
        _refresh_trust_material(target, value, state)
    return {
        "root": str(target),
        "issuers": sorted(issuers),
        "validity_days": CRL_VALIDITY_DAYS,
        "refreshed_at": datetime.now(UTC).isoformat(),
    }


def _existing_lab_roles(root: Path, agent_id: str) -> dict[str, Any] | None:
    central = root / "roles" / "central"
    edge = root / "roles" / "edge"
    metadata_paths = (
        central / "role-metadata.json",
        edge / "role-metadata.json",
    )
    present = tuple(path.is_file() for path in metadata_paths)
    if not any(present):
        return None
    if not all(present):
        raise PkiError("role export metadata is only partially present")
    try:
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_paths]
    except (OSError, json.JSONDecodeError) as exc:
        raise PkiError("role export metadata is invalid") from exc
    if documents[0] != documents[1] or documents[0].get("agent_id") != agent_id:
        raise PkiError("role export metadata is inconsistent with the Agent")
    required = (
        central / "https/server/chain.pem",
        central / "https/server/key.pem",
        central / f"https/agents/{agent_id}.pem",
        central / "https/trust/ca-crl.pem",
        central / "opcua/application/cert.pem",
        central / "opcua/application/key.pem",
        central / "opcua/user/cert.pem",
        central / "opcua/user/key.pem",
        central / "opcua/trust/servers/opcua-ca.pem",
        central / "opcua/crl/opcua-crl.pem",
        central / "opcua/pins/edge-cert.pem",
        edge / "https/client/chain.pem",
        edge / "https/client/key.pem",
        edge / "https/trust/ca-crl.pem",
        edge / "opcua/server/cert.pem",
        edge / "opcua/server/key.pem",
        edge / "opcua/trust/clients/opcua-ca.pem",
        edge / "opcua/crl/opcua-crl.pem",
        edge / "opcua/users/central-shadow.pem",
        edge / "local/health-hmac.key",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise PkiError("completed role metadata references missing runtime material")
    return {
        "central": str(central),
        "edge": str(edge),
        "health_key_id": documents[0]["health_key_id"],
    }


def bootstrap_lab(
    root: str | Path,
    *,
    central_host: str,
    edge_host: str,
    agent_id: str,
    central_ip: str | None = None,
    edge_ip: str | None = None,
    key_size: int = 3072,
) -> dict[str, Any]:
    requested = Path(root).expanduser()
    if not requested.exists() or not any(requested.iterdir()):
        initialize_pki(requested, key_size=key_size)
    target = requested.resolve(strict=True)
    state = _load_state(target)
    if int(state["key_size"]) != key_size:
        raise PkiError("existing PKI key size differs from the bootstrap request")
    common = {
        "central_https_server": issue_certificate(
            target,
            issuer="https",
            name="central-https-server",
            role="https-server",
            dns_names=(central_host,),
            ip_addresses=() if central_ip is None else (central_ip,),
        ),
        "agent_https_client": issue_certificate(
            target,
            issuer="https",
            name=f"{agent_id}-https-client",
            role="https-client",
            uri=f"urn:sang:agent:{agent_id}",
        ),
        "edge_opcua_server": issue_certificate(
            target,
            issuer="opcua",
            name=f"{agent_id}-opcua-server",
            role="opcua-server",
            uri=f"urn:sang:agent:{agent_id}",
            dns_names=(edge_host,),
            ip_addresses=() if edge_ip is None else (edge_ip,),
        ),
        "central_opcua_client": issue_certificate(
            target,
            issuer="opcua",
            name="central-opcua-client",
            role="opcua-client",
            uri="urn:sang:central:control-plane",
        ),
        "central_opcua_user": issue_certificate(
            target,
            issuer="opcua",
            name="central-opcua-shadow-user",
            role="opcua-user",
            uri="urn:sang:user:central-shadow",
        ),
    }
    roles = _existing_lab_roles(target, agent_id) or export_lab_roles(
        target,
        agent_id=agent_id,
        certificates=common,
    )
    return {"root": str(target), "certificates": common, "roles": roles}


def _copy_role_file(source: str | Path, destination: Path, *, private: bool) -> None:
    source_path = Path(source)
    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise PkiError(f"cannot read role material: {source_path}") from exc
    _write_atomic(destination, data, 0o600 if private else 0o644)


def export_lab_roles(
    root: str | Path,
    *,
    agent_id: str,
    certificates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    target = Path(root).expanduser().resolve(strict=True)
    roles_root = target / "roles"
    central = roles_root / "central"
    edge = roles_root / "edge"

    central_https = certificates["central_https_server"]
    agent_https = certificates["agent_https_client"]
    edge_opcua = certificates["edge_opcua_server"]
    central_opcua = certificates["central_opcua_client"]
    central_user = certificates["central_opcua_user"]

    copies = (
        (central_https["chain"], central / "https/server/chain.pem", False),
        (central_https["private_key"], central / "https/server/key.pem", True),
        (
            agent_https["certificate"],
            central / f"https/agents/{agent_id}.pem",
            False,
        ),
        (
            target / "bundles/https-ca-crl.pem",
            central / "https/trust/ca-crl.pem",
            False,
        ),
        (central_opcua["certificate"], central / "opcua/application/cert.pem", False),
        (central_opcua["private_key"], central / "opcua/application/key.pem", True),
        (central_user["certificate"], central / "opcua/user/cert.pem", False),
        (central_user["private_key"], central / "opcua/user/key.pem", True),
        (
            target / "trust/opcua/opcua-ca.pem",
            central / "opcua/trust/servers/opcua-ca.pem",
            False,
        ),
        (
            target / "crl/opcua/opcua-crl.pem",
            central / "opcua/crl/opcua-crl.pem",
            False,
        ),
        (edge_opcua["certificate"], central / "opcua/pins/edge-cert.pem", False),
        (agent_https["chain"], edge / "https/client/chain.pem", False),
        (agent_https["private_key"], edge / "https/client/key.pem", True),
        (
            target / "bundles/https-ca-crl.pem",
            edge / "https/trust/ca-crl.pem",
            False,
        ),
        (edge_opcua["certificate"], edge / "opcua/server/cert.pem", False),
        (edge_opcua["private_key"], edge / "opcua/server/key.pem", True),
        (
            target / "trust/opcua/opcua-ca.pem",
            edge / "opcua/trust/clients/opcua-ca.pem",
            False,
        ),
        (
            target / "crl/opcua/opcua-crl.pem",
            edge / "opcua/crl/opcua-crl.pem",
            False,
        ),
        (
            central_user["certificate"],
            edge / "opcua/users/central-shadow.pem",
            False,
        ),
    )
    for source, destination, private in copies:
        _copy_role_file(source, destination, private=private)

    health_key_id = f"{agent_id}-health-01"
    health_key = edge / "local/health-hmac.key"
    if health_key.exists():
        mode = health_key.stat().st_mode & 0o777
        value = health_key.read_text(encoding="ascii").strip()
        if mode & 0o077 or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise PkiError("existing Edge health HMAC key is invalid")
    else:
        _write_atomic(
            health_key,
            secrets.token_hex(32).encode("ascii") + b"\n",
            0o600,
        )
    metadata = {
        "schema_version": 1,
        "agent_id": agent_id,
        "health_key_id": health_key_id,
        "central": {
            "https_server_certificate_sha256": central_https["certificate_sha256"],
            "opcua_application_certificate_sha256": central_opcua[
                "certificate_sha256"
            ],
            "opcua_user_certificate_sha256": central_user["certificate_sha256"],
        },
        "edge": {
            "https_client_certificate_sha256": agent_https["certificate_sha256"],
            "https_client_not_before": agent_https["not_before"],
            "https_client_not_after": agent_https["not_after"],
            "opcua_server_certificate_sha256": edge_opcua["certificate_sha256"],
        },
    }
    _write_json(central / "role-metadata.json", metadata, 0o644)
    _write_json(edge / "role-metadata.json", metadata, 0o644)
    return {
        "central": str(central),
        "edge": str(edge),
        "health_key_id": health_key_id,
    }


def _existing_https_agent_roles(
    root: Path,
    agent_id: str,
) -> dict[str, Any] | None:
    role_root = root / "roles" / "agents" / agent_id
    central = role_root / "central"
    edge = role_root / "edge"
    metadata_paths = (
        central / "role-metadata.json",
        edge / "role-metadata.json",
    )
    present = tuple(path.is_file() for path in metadata_paths)
    if not any(present):
        if role_root.exists() and any(role_root.iterdir()):
            raise PkiError("per-Agent role export is only partially present")
        return None
    if not all(present):
        raise PkiError("per-Agent role export metadata is only partially present")
    try:
        documents = [
            json.loads(path.read_text(encoding="utf-8")) for path in metadata_paths
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise PkiError("per-Agent role export metadata is invalid") from exc
    if documents[0] != documents[1] or documents[0].get("agent_id") != agent_id:
        raise PkiError("per-Agent role export metadata is inconsistent")

    central_certificate = central / f"https/agents/{agent_id}.pem"
    edge_chain = edge / "https/client/chain.pem"
    edge_key = edge / "https/client/key.pem"
    required = (
        central_certificate,
        edge_chain,
        edge_key,
        edge / "https/trust/ca-crl.pem",
    )
    if any(not path.is_file() for path in required):
        raise PkiError("completed per-Agent role export references missing material")
    if list(role_root.rglob("ca-key.pem")):
        raise PkiError("per-Agent role export must not contain CA private keys")

    try:
        certificate = load_certificate(central_certificate)
        edge_certificate = load_certificate(edge_chain)
        require_identity_uri(certificate, f"urn:sang:agent:{agent_id}")
        fingerprint = certificate_sha256(certificate)
        if fingerprint != certificate_sha256(edge_certificate):
            raise PkiError("central and Edge Agent certificates differ")
        if documents[0]["edge"]["https_client_certificate_sha256"] != fingerprint:
            raise PkiError("per-Agent certificate fingerprint differs from metadata")
        key = _load_private_key(edge_key)
        if key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            raise PkiError("per-Agent certificate and private key do not match")
    except (CertificateError, KeyError, OSError) as exc:
        raise PkiError("per-Agent role export material is invalid") from exc
    return {
        "agent_id": agent_id,
        "certificate_sha256": fingerprint,
        "central": str(central),
        "edge": str(edge),
    }


def _provision_https_agent_locked(
    target: Path,
    agent_id: str,
) -> dict[str, Any]:
    existing = _existing_https_agent_roles(target, agent_id)
    if existing is not None:
        return existing

    certificate = issue_certificate(
        target,
        issuer="https",
        name=f"{agent_id}-https-client",
        role="https-client",
        uri=f"urn:sang:agent:{agent_id}",
    )
    roles_parent = target / "roles" / "agents"
    roles_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    role_root = roles_parent / agent_id
    staging = Path(tempfile.mkdtemp(prefix=f".{agent_id}.", dir=roles_parent))
    central = staging / "central"
    edge = staging / "edge"
    metadata = {
        "schema_version": 1,
        "agent_id": agent_id,
        "edge": {
            "https_client_certificate_sha256": certificate["certificate_sha256"],
            "https_client_not_before": certificate["not_before"],
            "https_client_not_after": certificate["not_after"],
        },
    }
    try:
        _copy_role_file(
            certificate["certificate"],
            central / f"https/agents/{agent_id}.pem",
            private=False,
        )
        _copy_role_file(
            certificate["chain"],
            edge / "https/client/chain.pem",
            private=False,
        )
        _copy_role_file(
            certificate["private_key"],
            edge / "https/client/key.pem",
            private=True,
        )
        _copy_role_file(
            target / "bundles/https-ca-crl.pem",
            edge / "https/trust/ca-crl.pem",
            private=False,
        )
        _write_json(central / "role-metadata.json", metadata, 0o644)
        _write_json(edge / "role-metadata.json", metadata, 0o644)
        try:
            staging.rename(role_root)
        except FileExistsError:
            existing = _existing_https_agent_roles(target, agent_id)
            if existing is None:
                raise PkiError("per-Agent role export appeared concurrently")
            return existing
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "agent_id": agent_id,
        "certificate_sha256": certificate["certificate_sha256"],
        "central": str(role_root / "central"),
        "edge": str(role_root / "edge"),
    }


def provision_https_agent(
    root: str | Path,
    *,
    agent_id: str,
) -> dict[str, Any]:
    """Issue and export one independent HTTPS Agent identity."""
    if not SAFE_NAME.fullmatch(agent_id) or len(agent_id) > 64:
        raise PkiError("agent_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    target = Path(root).expanduser().resolve(strict=True)
    lock_path = target / ".provision-agent.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _provision_https_agent_locked(target, agent_id)


def _copy_immutable_role_file(
    source: str | Path,
    destination: Path,
    *,
    private: bool,
) -> None:
    source_path = Path(source)
    try:
        expected = source_path.read_bytes()
    except OSError as exc:
        raise PkiError(f"cannot read role material: {source_path}") from exc
    if destination.exists():
        try:
            actual = destination.read_bytes()
        except OSError as exc:
            raise PkiError(f"cannot read existing role material: {destination}") from exc
        if actual != expected:
            raise PkiError(f"existing role material differs: {destination}")
        mode = destination.stat().st_mode & 0o777
        if private and mode & 0o077:
            raise PkiError(f"existing private role material is too permissive: {destination}")
        return
    _write_atomic(destination, expected, 0o600 if private else 0o644)


def _central_opcua_runtime_material(target: Path) -> dict[str, dict[str, Any]]:
    application = issue_certificate(
        target,
        issuer="opcua",
        name="central-opcua-client",
        role="opcua-client",
        uri="urn:sang:central:control-plane",
    )
    user = issue_certificate(
        target,
        issuer="opcua",
        name="central-opcua-shadow-user",
        role="opcua-user",
        uri="urn:sang:user:central-shadow",
    )
    central = target / "roles/central"
    immutable = (
        (application["certificate"], central / "opcua/application/cert.pem", False),
        (application["private_key"], central / "opcua/application/key.pem", True),
        (user["certificate"], central / "opcua/user/cert.pem", False),
        (user["private_key"], central / "opcua/user/key.pem", True),
        (
            target / "trust/opcua/opcua-ca.pem",
            central / "opcua/trust/servers/opcua-ca.pem",
            False,
        ),
    )
    for source, destination, private in immutable:
        _copy_immutable_role_file(source, destination, private=private)
    _copy_role_file(
        target / "crl/opcua/opcua-crl.pem",
        central / "opcua/crl/opcua-crl.pem",
        private=False,
    )
    return {"application": application, "user": user}


def _endpoint_host_sans(value: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    host = value.strip()
    if not host or len(host) > 253 or "/" in host:
        raise PkiError("edge_host must be one DNS name, IPv4 address, or IPv6 address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip(".").lower().split(".")
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        ):
            raise PkiError("edge_host is not a valid DNS name")
        canonical = ".".join(labels)
        return canonical, (canonical,), ()
    canonical = str(address)
    return canonical, (), (canonical,)


def _runtime_role_result(
    target: Path,
    *,
    agent_id: str,
    edge_host: str,
    repair_missing_metadata: bool,
) -> dict[str, Any] | None:
    role_root = target / "roles/agents" / agent_id
    central = role_root / "central"
    edge = role_root / "edge"
    metadata_paths = (
        central / "runtime-metadata.json",
        edge / "runtime-metadata.json",
    )
    present = tuple(path.is_file() for path in metadata_paths)
    if not any(present):
        return None
    documents = []
    try:
        for path, exists in zip(metadata_paths, present, strict=True):
            if exists:
                documents.append(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise PkiError("per-Agent runtime metadata is invalid") from exc
    metadata = documents[0]
    if any(document != metadata for document in documents[1:]):
        raise PkiError("per-Agent runtime metadata is inconsistent")
    central_metadata = metadata.get("central")
    expected_server_ref = f"secret://opcua/pins/{agent_id}.pem"
    if (
        metadata.get("schema_version") != 1
        or metadata.get("agent_id") != agent_id
        or metadata.get("endpoint_host") != edge_host
        or metadata.get("application_uri") != f"urn:sang:agent:{agent_id}"
        or metadata.get("health_key_id") != f"{agent_id}-health-01"
        or not isinstance(central_metadata, dict)
        or central_metadata.get("server_certificate_ref") != expected_server_ref
    ):
        raise PkiError("per-Agent runtime metadata differs from the request")

    server_certificate = edge / "opcua/server/cert.pem"
    server_key = edge / "opcua/server/key.pem"
    health_key = edge / "local/health-hmac.key"
    server_pin = target / "roles/central/opcua/pins" / f"{agent_id}.pem"
    required = (
        server_certificate,
        server_key,
        edge / "opcua/trust/clients/opcua-ca.pem",
        edge / "opcua/crl/opcua-crl.pem",
        edge / "opcua/users/central-shadow.pem",
        health_key,
        central / "opcua/pins" / f"{agent_id}.pem",
        server_pin,
        target / "roles/central/opcua/application/cert.pem",
        target / "roles/central/opcua/application/key.pem",
        target / "roles/central/opcua/user/cert.pem",
        target / "roles/central/opcua/user/key.pem",
        target / "roles/central/opcua/trust/servers/opcua-ca.pem",
        target / "roles/central/opcua/crl/opcua-crl.pem",
    )
    if any(not path.is_file() for path in required):
        raise PkiError("completed per-Agent runtime metadata references missing material")
    try:
        certificate = load_certificate(server_certificate)
        require_identity_uri(certificate, f"urn:sang:agent:{agent_id}")
        fingerprint = certificate_sha256(certificate)
        if metadata["edge"]["opcua_server_certificate_sha256"] != fingerprint:
            raise PkiError("OPC UA server fingerprint differs from runtime metadata")
        if certificate_sha256(load_certificate(server_pin)) != fingerprint:
            raise PkiError("Central OPC UA server pin differs from the Edge certificate")
        if certificate_sha256(
            load_certificate(central / "opcua/pins" / f"{agent_id}.pem")
        ) != fingerprint:
            raise PkiError("per-Agent Central pin differs from the Edge certificate")
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        try:
            expected_ip = ipaddress.ip_address(edge_host)
        except ValueError:
            if edge_host not in san.get_values_for_type(x509.DNSName):
                raise PkiError("OPC UA server certificate DNS SAN differs from endpoint_host")
        else:
            if expected_ip not in san.get_values_for_type(x509.IPAddress):
                raise PkiError("OPC UA server certificate IP SAN differs from endpoint_host")
        key = _load_private_key(server_key)
        if key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            raise PkiError("OPC UA server certificate and private key do not match")
        health_value = health_key.read_text(encoding="ascii").strip()
    except (CertificateError, KeyError, OSError, UnicodeError) as exc:
        raise PkiError("per-Agent runtime role material is invalid") from exc
    if (
        health_key.stat().st_mode & 0o077
        or len(health_value) != 64
        or any(character not in "0123456789abcdef" for character in health_value)
    ):
        raise PkiError("per-Agent local health HMAC key is invalid")
    if repair_missing_metadata and not all(present):
        missing_path = metadata_paths[present.index(False)]
        _write_json(missing_path, metadata, 0o644)
    return {
        "agent_id": agent_id,
        "application_uri": metadata["application_uri"],
        "certificate_sha256": fingerprint,
        "central": str(central),
        "edge": str(edge),
        "health_key_id": metadata["health_key_id"],
        "server_certificate_ref": expected_server_ref,
    }


def provision_agent_runtime(
    root: str | Path,
    *,
    agent_id: str,
    edge_host: str,
) -> dict[str, Any]:
    """Provision one isolated HTTPS Agent and OPC UA Gateway runtime."""
    if not SAFE_NAME.fullmatch(agent_id) or len(agent_id) > 64:
        raise PkiError("agent_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    canonical_host, dns_names, ip_addresses = _endpoint_host_sans(edge_host)
    target = Path(root).expanduser().resolve(strict=True)
    lock_path = target / ".provision-agent.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _provision_https_agent_locked(target, agent_id)
        central_material = _central_opcua_runtime_material(target)
        existing = _runtime_role_result(
            target,
            agent_id=agent_id,
            edge_host=canonical_host,
            repair_missing_metadata=True,
        )
        if existing is not None:
            return existing

        server = issue_certificate(
            target,
            issuer="opcua",
            name=f"{agent_id}-opcua-server",
            role="opcua-server",
            uri=f"urn:sang:agent:{agent_id}",
            dns_names=dns_names,
            ip_addresses=ip_addresses,
        )
        role_root = target / "roles/agents" / agent_id
        central = role_root / "central"
        edge = role_root / "edge"
        immutable = (
            (server["certificate"], edge / "opcua/server/cert.pem", False),
            (server["private_key"], edge / "opcua/server/key.pem", True),
            (
                target / "trust/opcua/opcua-ca.pem",
                edge / "opcua/trust/clients/opcua-ca.pem",
                False,
            ),
            (
                central_material["user"]["certificate"],
                edge / "opcua/users/central-shadow.pem",
                False,
            ),
            (
                server["certificate"],
                central / "opcua/pins" / f"{agent_id}.pem",
                False,
            ),
            (
                server["certificate"],
                target / "roles/central/opcua/pins" / f"{agent_id}.pem",
                False,
            ),
        )
        for source, destination, private in immutable:
            _copy_immutable_role_file(source, destination, private=private)
        _copy_role_file(
            target / "crl/opcua/opcua-crl.pem",
            edge / "opcua/crl/opcua-crl.pem",
            private=False,
        )

        health_key = edge / "local/health-hmac.key"
        if health_key.exists():
            value = health_key.read_text(encoding="ascii").strip()
            if (
                health_key.stat().st_mode & 0o077
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise PkiError("existing per-Agent local health HMAC key is invalid")
        else:
            _write_atomic(health_key, secrets.token_hex(32).encode("ascii") + b"\n", 0o600)

        server_ref = f"secret://opcua/pins/{agent_id}.pem"
        metadata = {
            "schema_version": 1,
            "agent_id": agent_id,
            "endpoint_host": canonical_host,
            "application_uri": f"urn:sang:agent:{agent_id}",
            "health_key_id": f"{agent_id}-health-01",
            "central": {"server_certificate_ref": server_ref},
            "edge": {
                "opcua_server_certificate_sha256": server["certificate_sha256"],
                "opcua_server_not_before": server["not_before"],
                "opcua_server_not_after": server["not_after"],
            },
        }
        _write_json(central / "runtime-metadata.json", metadata, 0o644)
        _write_json(edge / "runtime-metadata.json", metadata, 0o644)
        result = _runtime_role_result(
            target,
            agent_id=agent_id,
            edge_host=canonical_host,
            repair_missing_metadata=False,
        )
        if result is None:
            raise PkiError("per-Agent runtime activation did not complete")
        return result


def activate_agent_https_certificate(
    root: str | Path,
    *,
    agent_id: str,
    certificate: dict[str, Any],
) -> dict[str, Any]:
    """Atomically activate an already-issued Agent mTLS certificate in role exports."""
    target = Path(root).expanduser().resolve(strict=True)
    expected_uri = f"urn:sang:agent:{agent_id}"
    if (
        certificate.get("issuer") != "https"
        or certificate.get("role") != "https-client"
        or certificate.get("uri") != expected_uri
        or certificate.get("status") != "active"
    ):
        raise PkiError("rotation certificate is not an active HTTPS Agent identity")
    state = _load_state(target)
    registered = state["issued"].get(str(certificate.get("serial_number")))
    if registered is None or registered.get("certificate_sha256") != certificate.get(
        "certificate_sha256"
    ):
        raise PkiError("rotation certificate is not registered in this PKI")

    central = target / "roles" / "central"
    edge = target / "roles" / "edge"
    metadata_paths = (
        central / "role-metadata.json",
        edge / "role-metadata.json",
    )
    try:
        metadata_documents = [
            json.loads(path.read_text(encoding="utf-8")) for path in metadata_paths
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise PkiError("role export metadata is missing or invalid") from exc
    if any(document.get("agent_id") != agent_id for document in metadata_documents):
        raise PkiError("role export Agent identity differs from the rotation request")

    previous_fingerprint = metadata_documents[0]["edge"][
        "https_client_certificate_sha256"
    ]
    previous = next(
        (
            value
            for value in state["issued"].values()
            if value.get("certificate_sha256") == previous_fingerprint
        ),
        None,
    )
    _copy_role_file(
        certificate["certificate"],
        central / f"https/agents/{agent_id}.pem",
        private=False,
    )
    _copy_role_file(
        certificate["chain"],
        edge / "https/client/chain.pem",
        private=False,
    )
    _copy_role_file(
        certificate["private_key"],
        edge / "https/client/key.pem",
        private=True,
    )
    for path, document in zip(metadata_paths, metadata_documents, strict=True):
        document["edge"]["https_client_certificate_sha256"] = certificate[
            "certificate_sha256"
        ]
        document["edge"]["https_client_not_before"] = certificate["not_before"]
        document["edge"]["https_client_not_after"] = certificate["not_after"]
        _write_json(path, document, 0o644)
    return {
        "agent_id": agent_id,
        "certificate_sha256": certificate["certificate_sha256"],
        "previous_certificate_sha256": previous_fingerprint,
        "previous_certificate": (
            None
            if previous is None
            else str(
                target
                / "issued"
                / previous["issuer"]
                / previous["name"]
                / "cert.pem"
            )
        ),
        "edge_role": str(edge),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the SANG lab PKI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init")
    initialize.add_argument("--root", type=Path, required=True)
    initialize.add_argument("--common-name", default="SANG Lab")

    issue = subparsers.add_parser("issue")
    issue.add_argument("--root", type=Path, required=True)
    issue.add_argument("--issuer", choices=sorted(ISSUER_ROLES), required=True)
    issue.add_argument("--name", required=True)
    issue.add_argument("--role", choices=sorted(set().union(*ISSUER_ROLES.values())), required=True)
    issue.add_argument("--uri")
    issue.add_argument("--dns", action="append", default=[])
    issue.add_argument("--ip", action="append", default=[])
    issue.add_argument("--days", type=int, default=365)

    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--root", type=Path, required=True)
    revoke.add_argument("--certificate", type=Path, required=True)
    revoke.add_argument("--reason", required=True)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--certificate", type=Path, required=True)

    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--root", type=Path, required=True)
    refresh.add_argument("--issuer", choices=("all", *sorted(ISSUER_ROLES)), default="all")

    bootstrap = subparsers.add_parser("bootstrap-lab")
    bootstrap.add_argument("--root", type=Path, required=True)
    bootstrap.add_argument("--central-host", required=True)
    bootstrap.add_argument("--edge-host", required=True)
    bootstrap.add_argument("--agent-id", required=True)
    bootstrap.add_argument("--central-ip")
    bootstrap.add_argument("--edge-ip")

    provision = subparsers.add_parser("provision-agent")
    provision.add_argument("--root", type=Path, required=True)
    provision.add_argument("--agent-id", required=True)

    runtime = subparsers.add_parser("provision-runtime")
    runtime.add_argument("--root", type=Path, required=True)
    runtime.add_argument("--agent-id", required=True)
    runtime.add_argument("--edge-host", required=True)
    return parser


def main(args: list[str] | None = None) -> None:
    options = _parser().parse_args(args)
    if options.command == "init":
        result = initialize_pki(options.root, common_name=options.common_name)
    elif options.command == "issue":
        result = issue_certificate(
            options.root,
            issuer=options.issuer,
            name=options.name,
            role=options.role,
            uri=options.uri,
            dns_names=tuple(options.dns),
            ip_addresses=tuple(options.ip),
            days=options.days,
        )
    elif options.command == "revoke":
        result = revoke_certificate(
            options.root,
            certificate_path=options.certificate,
            reason=options.reason,
        )
    elif options.command == "inspect":
        certificate = load_certificate(options.certificate)
        result = {
            "certificate_sha256": certificate_sha256(certificate),
            "serial_number": str(certificate.serial_number),
            "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "not_before": certificate.not_valid_before_utc.isoformat(),
            "not_after": certificate.not_valid_after_utc.isoformat(),
        }
    elif options.command == "refresh":
        result = refresh_crls(
            options.root,
            issuer=None if options.issuer == "all" else options.issuer,
        )
    elif options.command == "bootstrap-lab":
        result = bootstrap_lab(
            options.root,
            central_host=options.central_host,
            edge_host=options.edge_host,
            agent_id=options.agent_id,
            central_ip=options.central_ip,
            edge_ip=options.edge_ip,
        )
    elif options.command == "provision-agent":
        result = provision_https_agent(
            options.root,
            agent_id=options.agent_id,
        )
    else:
        result = provision_agent_runtime(
            options.root,
            agent_id=options.agent_id,
            edge_host=options.edge_host,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
