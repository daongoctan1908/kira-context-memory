"""Argon2id password adapter tests."""

import pytest

from app.domain.errors.auth import AuthStoreError
from app.infrastructure.auth import PwdlibPasswordHasher


def test_pwdlib_hashes_and_verifies_argon2id_passwords() -> None:
    hasher = PwdlibPasswordHasher()
    password_hash = hasher.hash("a-valid-password")

    valid, replacement = hasher.verify_and_update("a-valid-password", password_hash)

    assert password_hash.startswith("$argon2id$")
    assert valid
    assert replacement is None
    assert hasher.verify_and_update("wrong-password", password_hash)[0] is False


def test_pwdlib_rejects_unknown_hash_without_exposing_it() -> None:
    with pytest.raises(AuthStoreError) as captured:
        PwdlibPasswordHasher().verify_and_update("password", "not-a-supported-hash")
    assert str(captured.value) == ""
