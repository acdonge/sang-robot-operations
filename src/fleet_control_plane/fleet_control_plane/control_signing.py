"""Ed25519 signing and rotation-aware verification for delegated leases."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel

from fleet_control_plane.orchestration_store import canonical_json
from fleet_control_plane.status import parse_datetime


class SignatureInvalid(ValueError):
    pass


SignedContract = TypeVar("SignedContract", bound=BaseModel)


def _payload_bytes(model: BaseModel) -> bytes:
    return canonical_json(
        model.model_dump(mode="json", exclude={"signature"})
    ).encode("utf-8")


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls, key_id: str) -> "SigningKey":
        return cls(key_id=key_id, private_key=Ed25519PrivateKey.generate())

    def public_key_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def sign_contract(self, contract: SignedContract) -> SignedContract:
        issuer_key_id = getattr(contract, "issuer_key_id", None)
        if issuer_key_id != self.key_id:
            raise SignatureInvalid(
                f"contract issuer key {issuer_key_id!r} does not match {self.key_id!r}"
            )
        signature = self.private_key.sign(_payload_bytes(contract))
        return contract.model_copy(
            update={"signature": base64.b64encode(signature).decode("ascii")}
        )

    def sign_payload(self, payload: BaseModel) -> str:
        signature = self.private_key.sign(_payload_bytes(payload))
        return base64.b64encode(signature).decode("ascii")


@dataclass(frozen=True)
class VerificationKey:
    key_id: str
    public_key: Ed25519PublicKey
    valid_from: datetime
    valid_until: datetime | None = None
    revoked_at: datetime | None = None

    def active_at(self, at: datetime) -> bool:
        current = parse_datetime(at)
        if current < parse_datetime(self.valid_from):
            return False
        if self.valid_until is not None and current >= parse_datetime(self.valid_until):
            return False
        if self.revoked_at is not None and current >= parse_datetime(self.revoked_at):
            return False
        return True


class VerificationKeyRing:
    def __init__(self) -> None:
        self._keys: dict[str, VerificationKey] = {}

    def add(
        self,
        *,
        key_id: str,
        public_key_b64: str,
        valid_from: datetime,
        valid_until: datetime | None = None,
        revoked_at: datetime | None = None,
    ) -> None:
        starts = parse_datetime(valid_from)
        ends = None if valid_until is None else parse_datetime(valid_until)
        revoked = None if revoked_at is None else parse_datetime(revoked_at)
        if ends is not None and ends <= starts:
            raise SignatureInvalid("signing key validity window is empty")
        if revoked is not None and revoked < starts:
            raise SignatureInvalid("signing key cannot be revoked before it becomes valid")
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, TypeError) as exc:
            raise SignatureInvalid("invalid Ed25519 public key") from exc
        candidate = VerificationKey(
            key_id=key_id,
            public_key=public_key,
            valid_from=starts,
            valid_until=ends,
            revoked_at=revoked,
        )
        existing = self._keys.get(key_id)
        if existing is not None:
            existing_raw = existing.public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if (
                existing_raw != raw
                or existing.valid_from != candidate.valid_from
                or existing.valid_until != candidate.valid_until
                or existing.revoked_at != candidate.revoked_at
            ):
                raise SignatureInvalid("signing key identity is immutable")
            return
        self._keys[key_id] = candidate

    def revoke(self, key_id: str, *, revoked_at: datetime) -> None:
        existing = self._keys.get(key_id)
        if existing is None:
            raise SignatureInvalid(f"unknown signing key: {key_id}")
        revoked = parse_datetime(revoked_at)
        if revoked < existing.valid_from:
            raise SignatureInvalid("signing key cannot be revoked before it becomes valid")
        if existing.revoked_at is not None:
            if revoked != existing.revoked_at:
                raise SignatureInvalid("signing key revocation is immutable")
            return
        self._keys[key_id] = VerificationKey(
            key_id=existing.key_id,
            public_key=existing.public_key,
            valid_from=existing.valid_from,
            valid_until=existing.valid_until,
            revoked_at=revoked,
        )

    def verify_contract(self, contract: BaseModel, *, at: datetime) -> None:
        key_id = getattr(contract, "issuer_key_id", None)
        signature_text = getattr(contract, "signature", None)
        if not isinstance(key_id, str) or not isinstance(signature_text, str):
            raise SignatureInvalid("contract is not a supported signed contract")
        key = self._keys.get(key_id)
        if key is None:
            raise SignatureInvalid(f"unknown signing key: {key_id}")
        if not key.active_at(at):
            raise SignatureInvalid(f"signing key is not active: {key_id}")
        try:
            signature = base64.b64decode(signature_text, validate=True)
            key.public_key.verify(signature, _payload_bytes(contract))
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SignatureInvalid("contract signature verification failed") from exc

    def verify_payload(
        self,
        *,
        key_id: str,
        signature_text: str,
        payload: BaseModel,
        at: datetime,
    ) -> None:
        key = self._keys.get(key_id)
        if key is None:
            raise SignatureInvalid(f"unknown signing key: {key_id}")
        if not key.active_at(at):
            raise SignatureInvalid(f"signing key is not active: {key_id}")
        try:
            signature = base64.b64decode(signature_text, validate=True)
            key.public_key.verify(signature, _payload_bytes(payload))
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SignatureInvalid("payload signature verification failed") from exc
