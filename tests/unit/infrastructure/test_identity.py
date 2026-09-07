import pytest

from app.infrastructure.identity import NullIdentityAdapter, StaticIdentityAdapter


async def test_static_identity_is_explicit_and_null_identity_fails_closed():
    assert (await StaticIdentityAdapter("dev-user").resolve()).user_id == "dev-user"
    assert await NullIdentityAdapter().resolve() is None


def test_static_identity_rejects_empty_user():
    with pytest.raises(ValueError):
        StaticIdentityAdapter(" ")
