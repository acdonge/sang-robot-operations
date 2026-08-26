"""Local human and service identity for the Central operations API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.status import isoformat_z, parse_datetime


Role = Literal["viewer", "operator", "admin"]
Password = Annotated[
    str,
    StringConstraints(min_length=12, max_length=256),
]
ROLE_LEVEL = {"viewer": 10, "operator": 20, "admin": 30}
SESSION_TTL = timedelta(hours=8)
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1


class AuthenticationError(ValueError):
    """Credentials are absent, invalid, expired, or revoked."""


class AuthorizationError(ValueError):
    """The authenticated principal lacks the required role."""


class IdentityConflict(ValueError):
    """An identity mutation conflicts with durable state."""


class LoginRequest(FleetModel):
    principal_id: SafeId
    password: Password


class UserCreate(FleetModel):
    principal_id: SafeId
    display_name: ShortText
    role: Role
    password: Password


class ServiceIdentityCreate(FleetModel):
    principal_id: SafeId
    display_name: ShortText
    role: Role
    token_id: SafeId
    expires_at: AwareDatetime | None = None
    agent_ids: list[SafeId] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def service_scope_is_coherent(self) -> "ServiceIdentityCreate":
        if len(self.agent_ids) != len(set(self.agent_ids)):
            raise ValueError("agent_ids must be unique")
        if self.role == "operator" and not self.agent_ids:
            raise ValueError("operator service identities require at least one agent_id")
        return self


class PrincipalUpdate(FleetModel):
    expected_revision: int = Field(ge=1)
    display_name: ShortText
    role: Role
    enabled: bool
    password: Password | None = None


class TokenRevokeRequest(FleetModel):
    reason: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
    ]


@dataclass(frozen=True)
class Principal:
    principal_id: str
    principal_kind: str
    display_name: str
    role: str
    auth_kind: str
    session_id: str | None = None

    def require(self, minimum_role: str) -> None:
        if ROLE_LEVEL[self.role] < ROLE_LEVEL[minimum_role]:
            raise AuthorizationError(
                f"{minimum_role} role is required; principal has {self.role}"
            )

    def public(self) -> dict[str, str]:
        return {
            "principal_id": self.principal_id,
            "principal_kind": self.principal_kind,
            "display_name": self.display_name,
            "role": self.role,
            "auth_kind": self.auth_kind,
        }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt-v1",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        version, n, r, p, salt_text, digest_text = encoded.split("$")
        if version != "scrypt-v1":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class AuthManager:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _public_principal(row: sqlite3.Row) -> dict[str, object]:
        return {
            "principal_id": row["principal_id"],
            "principal_kind": row["principal_kind"],
            "display_name": row["display_name"],
            "role": row["role"],
            "enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        occurred_at: datetime,
        principal_id: str | None,
        action: str,
        target_type: str,
        target_id: str | None,
        outcome: str,
        details: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO security_audit (
                occurred_at, principal_id, action, target_type,
                target_id, outcome, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                isoformat_z(occurred_at),
                principal_id,
                action,
                target_type,
                target_id,
                outcome,
                json.dumps(
                    details or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    def principal_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM principals").fetchone()
        return int(row["count"])

    def bootstrap_admin(
        self,
        *,
        principal_id: str,
        display_name: str,
        password: str,
        occurred_at: datetime,
    ) -> dict[str, object]:
        body = UserCreate(
            principal_id=principal_id,
            display_name=display_name,
            role="admin",
            password=password,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM principals"
            ).fetchone()["count"]
            if count:
                raise IdentityConflict("bootstrap is allowed only before the first identity")
            result = self._insert_user(
                connection,
                body,
                occurred_at=occurred_at,
                actor_id=principal_id,
                action="bootstrap_admin",
            )
        return result

    def _insert_user(
        self,
        connection: sqlite3.Connection,
        body: UserCreate,
        *,
        occurred_at: datetime,
        actor_id: str,
        action: str = "create_user",
    ) -> dict[str, object]:
        timestamp = isoformat_z(occurred_at)
        try:
            connection.execute(
                """
                INSERT INTO principals (
                    principal_id, principal_kind, display_name, role,
                    secret_hash, enabled, revision, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    body.principal_id,
                    body.display_name,
                    body.role,
                    hash_password(body.password),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IdentityConflict(str(exc)) from exc
        self._audit(
            connection,
            occurred_at=occurred_at,
            principal_id=actor_id,
            action=action,
            target_type="principal",
            target_id=body.principal_id,
            outcome="success",
            details={"role": body.role, "principal_kind": "user"},
        )
        row = connection.execute(
            "SELECT * FROM principals WHERE principal_id = ?",
            (body.principal_id,),
        ).fetchone()
        return self._public_principal(row)

    def create_user(
        self,
        body: UserCreate,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._insert_user(
                connection,
                body,
                occurred_at=occurred_at,
                actor_id=actor_id,
            )

    def create_service_identity(
        self,
        body: ServiceIdentityCreate,
        *,
        actor_id: str,
        occurred_at: datetime,
        raw_token: str | None = None,
    ) -> dict[str, object]:
        timestamp = isoformat_z(occurred_at)
        if raw_token is None:
            raw_token = "sang_pat_" + secrets.token_urlsafe(32)
        if not raw_token.startswith("sang_pat_") or len(raw_token) < 32:
            raise ValueError("service token does not satisfy the SANG token contract")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO principals (
                        principal_id, principal_kind, display_name, role,
                        secret_hash, enabled, revision, created_at, updated_at
                    ) VALUES (?, 'service', ?, ?, NULL, 1, 1, ?, ?)
                    """,
                    (
                        body.principal_id,
                        body.display_name,
                        body.role,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO service_tokens (
                        token_id, principal_id, token_sha256, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        body.token_id,
                        body.principal_id,
                        _token_hash(raw_token),
                        None if body.expires_at is None else isoformat_z(body.expires_at),
                        timestamp,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO principal_agent_scopes (principal_id, agent_id)
                    VALUES (?, ?)
                    """,
                    [
                        (body.principal_id, agent_id)
                        for agent_id in body.agent_ids
                    ],
                )
                self._audit(
                    connection,
                    occurred_at=occurred_at,
                    principal_id=actor_id,
                    action="create_service_identity",
                    target_type="principal",
                    target_id=body.principal_id,
                    outcome="success",
                    details={
                        "role": body.role,
                        "token_id": body.token_id,
                        "agent_ids": body.agent_ids,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise IdentityConflict(str(exc)) from exc
        return {
            "principal_id": body.principal_id,
            "principal_kind": "service",
            "display_name": body.display_name,
            "role": body.role,
            "enabled": True,
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "token_id": body.token_id,
            "token": raw_token,
            "expires_at": None if body.expires_at is None else isoformat_z(body.expires_at),
            "agent_ids": body.agent_ids,
        }

    def service_agent_ids(self, principal_id: str) -> frozenset[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT agent_id FROM principal_agent_scopes
                WHERE principal_id = ? ORDER BY agent_id
                """,
                (principal_id,),
            ).fetchall()
        return frozenset(row["agent_id"] for row in rows)

    def update_principal(
        self,
        principal_id: str,
        body: PrincipalUpdate,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if row is None:
                raise AuthenticationError(f"unknown principal: {principal_id}")
            if row["revision"] != body.expected_revision:
                raise IdentityConflict(
                    f"principal revision is {row['revision']}, not {body.expected_revision}"
                )
            removes_admin = (
                bool(row["enabled"])
                and row["role"] == "admin"
                and (not body.enabled or body.role != "admin")
            )
            if removes_admin:
                others = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM principals
                    WHERE principal_id != ? AND role = 'admin' AND enabled = 1
                    """,
                    (principal_id,),
                ).fetchone()["count"]
                if not others:
                    raise IdentityConflict("the last enabled administrator cannot be removed")
            if body.password is not None and row["principal_kind"] != "user":
                raise IdentityConflict("service identities do not have passwords")
            secret_hash = (
                row["secret_hash"]
                if body.password is None
                else hash_password(body.password)
            )
            timestamp = isoformat_z(occurred_at)
            connection.execute(
                """
                UPDATE principals
                SET display_name = ?, role = ?, enabled = ?, secret_hash = ?,
                    revision = revision + 1, updated_at = ?
                WHERE principal_id = ?
                """,
                (
                    body.display_name,
                    body.role,
                    int(body.enabled),
                    secret_hash,
                    timestamp,
                    principal_id,
                ),
            )
            if not body.enabled:
                connection.execute(
                    """
                    UPDATE browser_sessions SET revoked_at = ?
                    WHERE principal_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, principal_id),
                )
                connection.execute(
                    """
                    UPDATE service_tokens
                    SET revoked_at = ?, revocation_reason = 'principal disabled'
                    WHERE principal_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, principal_id),
                )
            self._audit(
                connection,
                occurred_at=occurred_at,
                principal_id=actor_id,
                action="update_principal",
                target_type="principal",
                target_id=principal_id,
                outcome="success",
                details={
                    "previous_revision": body.expected_revision,
                    "role": body.role,
                    "enabled": body.enabled,
                    "password_rotated": body.password is not None,
                },
            )
            updated = connection.execute(
                "SELECT * FROM principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            return self._public_principal(updated)

    def revoke_service_token(
        self,
        token_id: str,
        body: TokenRevokeRequest,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM service_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if row is None:
                raise AuthenticationError(f"unknown service token: {token_id}")
            if row["revoked_at"] is None:
                timestamp = isoformat_z(occurred_at)
                connection.execute(
                    """
                    UPDATE service_tokens
                    SET revoked_at = ?, revocation_reason = ?
                    WHERE token_id = ?
                    """,
                    (timestamp, body.reason, token_id),
                )
                self._audit(
                    connection,
                    occurred_at=occurred_at,
                    principal_id=actor_id,
                    action="revoke_service_token",
                    target_type="service_token",
                    target_id=token_id,
                    outcome="success",
                    details={"reason": body.reason},
                )
            updated = connection.execute(
                "SELECT * FROM service_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        return {
            "token_id": updated["token_id"],
            "principal_id": updated["principal_id"],
            "expires_at": updated["expires_at"],
            "created_at": updated["created_at"],
            "revoked_at": updated["revoked_at"],
            "revocation_reason": updated["revocation_reason"],
        }

    def authenticate_password(
        self,
        principal_id: str,
        password: str,
        *,
        occurred_at: datetime,
    ) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            valid = (
                row is not None
                and row["principal_kind"] == "user"
                and bool(row["enabled"])
                and verify_password(password, row["secret_hash"])
            )
            self._audit(
                connection,
                occurred_at=occurred_at,
                principal_id=principal_id if row is not None else None,
                action="login",
                target_type="session",
                target_id=None,
                outcome="success" if valid else "denied",
            )
        if not valid:
            raise AuthenticationError("invalid credentials")
        return row

    def create_session(
        self,
        principal_row: sqlite3.Row,
        *,
        occurred_at: datetime,
    ) -> tuple[Principal, str, str, datetime]:
        session_id = secrets.token_urlsafe(18)
        raw_session = secrets.token_urlsafe(32)
        raw_csrf = secrets.token_urlsafe(24)
        expires_at = occurred_at.astimezone(timezone.utc) + SESSION_TTL
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO browser_sessions (
                    session_id, principal_id, session_sha256, csrf_sha256,
                    expires_at, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    principal_row["principal_id"],
                    _token_hash(raw_session),
                    _token_hash(raw_csrf),
                    isoformat_z(expires_at),
                    isoformat_z(occurred_at),
                    isoformat_z(occurred_at),
                ),
            )
        principal = Principal(
            principal_id=principal_row["principal_id"],
            principal_kind=principal_row["principal_kind"],
            display_name=principal_row["display_name"],
            role=principal_row["role"],
            auth_kind="session",
            session_id=session_id,
        )
        return principal, raw_session, raw_csrf, expires_at

    def authenticate_session(
        self,
        raw_session: str,
        *,
        occurred_at: datetime,
    ) -> Principal:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT browser_sessions.*, principals.principal_kind,
                       principals.display_name, principals.role, principals.enabled
                FROM browser_sessions
                JOIN principals USING (principal_id)
                WHERE session_sha256 = ?
                """,
                (_token_hash(raw_session),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or not bool(row["enabled"])
                or parse_datetime(row["expires_at"]) <= occurred_at
            ):
                raise AuthenticationError("session is invalid or expired")
            connection.execute(
                "UPDATE browser_sessions SET last_seen_at = ? WHERE session_id = ?",
                (isoformat_z(occurred_at), row["session_id"]),
            )
        return Principal(
            principal_id=row["principal_id"],
            principal_kind=row["principal_kind"],
            display_name=row["display_name"],
            role=row["role"],
            auth_kind="session",
            session_id=row["session_id"],
        )

    def validate_csrf(self, session_id: str, value: str | None) -> None:
        if value is None:
            raise AuthorizationError("CSRF token is required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT csrf_sha256 FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["csrf_sha256"], _token_hash(value)):
            raise AuthorizationError("CSRF token is invalid")

    def authenticate_bearer(
        self,
        raw_token: str,
        *,
        occurred_at: datetime,
    ) -> Principal:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT service_tokens.*, principals.principal_kind,
                       principals.display_name, principals.role, principals.enabled
                FROM service_tokens
                JOIN principals USING (principal_id)
                WHERE token_sha256 = ?
                """,
                (_token_hash(raw_token),),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or not bool(row["enabled"])
            or (
                row["expires_at"] is not None
                and parse_datetime(row["expires_at"]) <= occurred_at
            )
        ):
            raise AuthenticationError("bearer token is invalid or expired")
        return Principal(
            principal_id=row["principal_id"],
            principal_kind=row["principal_kind"],
            display_name=row["display_name"],
            role=row["role"],
            auth_kind="bearer",
        )

    def revoke_session(
        self,
        session_id: str,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> None:
        with self._connect() as connection:
            timestamp = isoformat_z(occurred_at)
            connection.execute(
                "UPDATE browser_sessions SET revoked_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )
            self._audit(
                connection,
                occurred_at=occurred_at,
                principal_id=principal_id,
                action="logout",
                target_type="session",
                target_id=session_id,
                outcome="success",
            )

    def record_request(
        self,
        *,
        principal_id: str | None,
        method: str,
        path: str,
        status_code: int,
        occurred_at: datetime,
    ) -> None:
        outcome = "success" if status_code < 400 else "denied" if status_code in {401, 403} else "failed"
        with self._connect() as connection:
            self._audit(
                connection,
                occurred_at=occurred_at,
                principal_id=principal_id,
                action=f"http_{method.lower()}",
                target_type="api",
                target_id=path,
                outcome=outcome,
                details={"status_code": status_code},
            )

    def list_principals(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM principals ORDER BY display_name, principal_id"
            ).fetchall()
            scopes = connection.execute(
                """
                SELECT principal_id, agent_id FROM principal_agent_scopes
                ORDER BY principal_id, agent_id
                """
            ).fetchall()
        by_principal: dict[str, list[str]] = {}
        for scope in scopes:
            by_principal.setdefault(scope["principal_id"], []).append(scope["agent_id"])
        return [
            {
                **self._public_principal(row),
                "agent_ids": by_principal.get(row["principal_id"], []),
            }
            for row in rows
        ]

    def list_security_audit(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("audit limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM security_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "principal_id": row["principal_id"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "outcome": row["outcome"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]
