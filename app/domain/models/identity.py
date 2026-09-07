"""Trusted end-user identity models independent of any authentication mechanism."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """A stable user identity produced by a trusted identity adapter."""

    user_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("user_id must not be empty")
