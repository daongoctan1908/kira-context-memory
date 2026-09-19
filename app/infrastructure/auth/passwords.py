"""Argon2id password hashing through the maintained pwdlib facade."""

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.domain.errors.auth import AuthStoreError


class PwdlibPasswordHasher:
    def __init__(self, password_hash: PasswordHash | None = None) -> None:
        self._password_hash = password_hash or PasswordHash.recommended()

    def hash(self, password: str) -> str:
        return self._password_hash.hash(password)

    def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        try:
            return self._password_hash.verify_and_update(password, password_hash)
        except UnknownHashError:
            raise AuthStoreError from None
