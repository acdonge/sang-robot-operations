"""Resolve opaque secret references without permitting path escape."""

from __future__ import annotations

import os
from pathlib import Path
import re


SECRET_REFERENCE = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]*$")


class SecretReferenceError(ValueError):
    """A secret reference is malformed, unsafe or points at the wrong object."""


class SecretResolver:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise SecretReferenceError("secret root must be a directory")

    @staticmethod
    def validate(reference: str) -> str:
        if not SECRET_REFERENCE.fullmatch(reference):
            raise SecretReferenceError("secret reference must use secret://<canonical-path>")
        relative = reference.removeprefix("secret://")
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise SecretReferenceError("secret reference contains an unsafe path component")
        return relative

    def resolve(
        self,
        reference: str,
        *,
        kind: str = "file",
        private: bool = False,
    ) -> Path:
        relative = self.validate(reference)
        candidate = (self.root / relative).resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SecretReferenceError("secret reference escapes the configured root") from exc
        if kind == "file" and not candidate.is_file():
            raise SecretReferenceError("secret reference must resolve to a regular file")
        if kind == "directory" and not candidate.is_dir():
            raise SecretReferenceError("secret reference must resolve to a directory")
        if kind not in {"file", "directory"}:
            raise ValueError("kind must be file or directory")
        if private:
            if kind != "file":
                raise ValueError("only files may be marked private")
            mode = candidate.stat().st_mode & 0o777
            if mode & 0o077:
                raise SecretReferenceError(
                    f"private secret permissions must be 0600 or stricter, got {mode:04o}"
                )
            if candidate.stat().st_uid not in {0, os.geteuid()}:
                raise SecretReferenceError("private secret is owned by another user")
        return candidate
