"""Fail-closed asyncua security configuration for SANG Edge connections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Client, Server, ua
from asyncua.common.utils import ServiceError
from asyncua.crypto import security_policies
from asyncua.crypto.permission_rules import SimpleRoleRuleset
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from asyncua.server.user_managers import User, UserRole
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from OpenSSL import crypto

from sang_security.certificates import load_certificate, require_identity_uri
from sang_security.refs import SecretResolver


STRICT_CLIENT_OPTIONS = (
    CertificateValidatorOptions.TIME_RANGE
    | CertificateValidatorOptions.URI
    | CertificateValidatorOptions.KEY_USAGE
    | CertificateValidatorOptions.EXT_KEY_USAGE
    | CertificateValidatorOptions.TRUSTED
    | CertificateValidatorOptions.REVOKED
    | CertificateValidatorOptions.PEER_CLIENT
)
STRICT_SERVER_OPTIONS = (
    CertificateValidatorOptions.TIME_RANGE
    | CertificateValidatorOptions.URI
    | CertificateValidatorOptions.KEY_USAGE
    | CertificateValidatorOptions.EXT_KEY_USAGE
    | CertificateValidatorOptions.TRUSTED
    | CertificateValidatorOptions.REVOKED
    | CertificateValidatorOptions.PEER_SERVER
)


class OpcUaSecurityError(ValueError):
    """OPC UA security material is incomplete or violates the frozen profile."""


def _location_files(locations: list[Path], label: str) -> tuple[Path, ...]:
    files = []
    for location in locations:
        if location.is_file():
            files.append(location)
        elif location.is_dir():
            files.extend(
                path
                for path in location.iterdir()
                if path.is_file() and path.suffix.lower() in {".pem", ".der"}
            )
    if not files:
        raise OpcUaSecurityError(f"{label} contains no PEM or DER material")
    return tuple(files)


def _load_crl(path: Path) -> x509.CertificateRevocationList:
    data = path.read_bytes()
    try:
        if b"-----BEGIN X509 CRL-----" in data:
            return x509.load_pem_x509_crl(data)
        return x509.load_der_x509_crl(data)
    except ValueError as exc:
        raise OpcUaSecurityError(f"invalid CRL material: {path}") from exc


def _validated_crl_expirations(
    trust_locations: list[Path],
    crl_locations: list[Path],
    *,
    now: datetime | None = None,
) -> tuple[datetime, ...]:
    current = now or datetime.now(timezone.utc)
    issuers = [load_certificate(path) for path in _location_files(trust_locations, "trust")]
    expirations = []
    for path in _location_files(crl_locations, "CRL"):
        crl = _load_crl(path)
        if crl.last_update_utc > current or crl.next_update_utc is None:
            raise OpcUaSecurityError(f"CRL validity window is invalid: {path}")
        if crl.next_update_utc <= current:
            raise OpcUaSecurityError(f"CRL has expired: {path}")
        issuer = next(
            (certificate for certificate in issuers if certificate.subject == crl.issuer),
            None,
        )
        if issuer is None:
            raise OpcUaSecurityError(f"CRL issuer is not in the protocol trust list: {path}")
        public_key = issuer.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise OpcUaSecurityError("SANG v1 supports RSA-signed CRLs only")
        try:
            public_key.verify(
                crl.signature,
                crl.tbs_certlist_bytes,
                padding.PKCS1v15(),
                crl.signature_hash_algorithm,
            )
        except InvalidSignature as exc:
            raise OpcUaSecurityError(f"CRL signature is invalid: {path}") from exc
        expirations.append(crl.next_update_utc)
    return tuple(expirations)


class IssuerTrustStore(TrustStore):
    """Treat the protocol-specific issuing CA as the trust anchor."""

    def __init__(self, trust_locations: list[Path], crl_locations: list[Path]) -> None:
        super().__init__(trust_locations, crl_locations)
        self._crl_expirations: tuple[datetime, ...] = ()

    async def load(self) -> None:
        expirations = _validated_crl_expirations(
            self.trust_locations,
            self.crl_locations,
        )
        candidate = TrustStore(self.trust_locations, self.crl_locations)
        await candidate.load()
        candidate._trust_store.set_flags(crypto.X509StoreFlags.PARTIAL_CHAIN)
        self._trust_store = candidate._trust_store
        self._revoked_list = candidate._revoked_list
        self._crl_expirations = expirations

    def is_revoked(self, certificate: x509.Certificate) -> bool:
        if not self._crl_expirations or min(self._crl_expirations) <= datetime.now(
            timezone.utc
        ):
            raise ServiceError(ua.StatusCodes.BadCertificateRevocationUnknown)
        return super().is_revoked(certificate)


class ValidatingCertificateUserManager:
    """Authorize exact user certificates and re-check current trust on every login."""

    def __init__(self, trust_store: TrustStore) -> None:
        self._trust_store = trust_store
        self._users: dict[bytes, tuple[object, User]] = {}

    async def add_user(self, certificate_path: Path, name: str) -> None:
        certificate = load_certificate(certificate_path)
        encoded = certificate.public_bytes(serialization.Encoding.DER)
        self._users[encoded] = (
            certificate,
            User(role=UserRole.User, name=name),
        )

    def get_user(
        self,
        iserver,
        username: str | None = None,
        password: str | None = None,
        certificate=None,
    ) -> User | None:
        del iserver, username, password
        entry = self._users.get(certificate)
        if entry is None or not self._trust_store.validate(entry[0]):
            return None
        return entry[1]


def _material_files(directory: Path, label: str) -> tuple[Path, ...]:
    files = tuple(
        path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in {".pem", ".der"}
    )
    if not files:
        raise OpcUaSecurityError(f"{label} directory contains no PEM or DER material")
    return files


@dataclass(frozen=True)
class OpcUaServerSecurity:
    certificate: Path
    private_key: Path
    trust_directory: Path
    crl_directory: Path
    authorized_user_certificates: tuple[Path, ...]

    def validate(self, application_uri: str) -> None:
        require_identity_uri(load_certificate(self.certificate), application_uri)
        _material_files(self.trust_directory, "client trust")
        _material_files(self.crl_directory, "client CRL")
        if not self.authorized_user_certificates:
            raise OpcUaSecurityError("at least one X.509 user certificate is required")
        for certificate in self.authorized_user_certificates:
            load_certificate(certificate)

    @classmethod
    def from_secret_refs(
        cls,
        resolver: SecretResolver,
        *,
        certificate_ref: str,
        private_key_ref: str,
        trust_directory_ref: str,
        crl_directory_ref: str,
        authorized_user_certificate_refs: tuple[str, ...],
    ) -> "OpcUaServerSecurity":
        return cls(
            certificate=resolver.resolve(certificate_ref),
            private_key=resolver.resolve(private_key_ref, private=True),
            trust_directory=resolver.resolve(trust_directory_ref, kind="directory"),
            crl_directory=resolver.resolve(crl_directory_ref, kind="directory"),
            authorized_user_certificates=tuple(
                resolver.resolve(reference)
                for reference in authorized_user_certificate_refs
            ),
        )


@dataclass(frozen=True)
class OpcUaClientSecurity:
    application_uri: str
    expected_server_application_uri: str
    application_certificate: Path
    application_private_key: Path
    trust_directory: Path
    crl_directory: Path
    user_certificate: Path
    user_private_key: Path
    server_certificate: Path | None = None

    def validate(self) -> None:
        require_identity_uri(load_certificate(self.application_certificate), self.application_uri)
        _material_files(self.trust_directory, "server trust")
        _material_files(self.crl_directory, "server CRL")
        load_certificate(self.user_certificate)
        if self.server_certificate is not None:
            require_identity_uri(
                load_certificate(self.server_certificate),
                self.expected_server_application_uri,
            )

    @classmethod
    def from_secret_refs(
        cls,
        resolver: SecretResolver,
        *,
        application_uri: str,
        expected_server_application_uri: str,
        application_certificate_ref: str,
        application_private_key_ref: str,
        trust_directory_ref: str,
        crl_directory_ref: str,
        user_certificate_ref: str,
        user_private_key_ref: str,
        server_certificate_ref: str | None = None,
    ) -> "OpcUaClientSecurity":
        return cls(
            application_uri=application_uri,
            expected_server_application_uri=expected_server_application_uri,
            application_certificate=resolver.resolve(application_certificate_ref),
            application_private_key=resolver.resolve(
                application_private_key_ref,
                private=True,
            ),
            trust_directory=resolver.resolve(trust_directory_ref, kind="directory"),
            crl_directory=resolver.resolve(crl_directory_ref, kind="directory"),
            user_certificate=resolver.resolve(user_certificate_ref),
            user_private_key=resolver.resolve(user_private_key_ref, private=True),
            server_certificate=(
                None
                if server_certificate_ref is None
                else resolver.resolve(server_certificate_ref)
            ),
        )


async def create_secure_server(
    security: OpcUaServerSecurity,
    *,
    application_uri: str,
) -> tuple[Server, TrustStore]:
    security.validate(application_uri)
    trust_store = IssuerTrustStore(
        [security.trust_directory],
        [security.crl_directory],
    )
    await trust_store.load()
    user_manager = ValidatingCertificateUserManager(trust_store)
    for index, path in enumerate(security.authorized_user_certificates, start=1):
        await user_manager.add_user(path, f"central-reader-{index}")
    server = Server(user_manager=user_manager)
    await server.init()
    await server.load_certificate(security.certificate)
    await server.load_private_key(security.private_key)
    server.set_certificate_validator(
        CertificateValidator(STRICT_CLIENT_OPTIONS, trust_store)
    )
    server.set_security_policy(
        [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt],
        permission_ruleset=SimpleRoleRuleset(),
    )
    server.set_identity_tokens([ua.X509IdentityToken])
    return server, trust_store


async def configure_secure_client(
    client: Client,
    security: OpcUaClientSecurity,
) -> None:
    security.validate()
    trust_store = IssuerTrustStore(
        [security.trust_directory],
        [security.crl_directory],
    )
    await trust_store.load()
    validator = CertificateValidator(STRICT_SERVER_OPTIONS, trust_store)

    async def validate_server(certificate, application) -> None:
        if application.ApplicationUri != security.expected_server_application_uri:
            raise ServiceError(ua.StatusCodes.BadCertificateUriInvalid)
        await validator(certificate, application)

    client.application_uri = security.application_uri
    client.name = "SANG Central OPC UA Shadow"
    client.description = client.name
    client.certificate_validator = validate_server
    await client.set_security(
        security_policies.SecurityPolicyBasic256Sha256,
        security.application_certificate,
        security.application_private_key,
        server_certificate=security.server_certificate,
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    await client.load_client_certificate(str(security.user_certificate))
    await client.load_private_key(security.user_private_key)
