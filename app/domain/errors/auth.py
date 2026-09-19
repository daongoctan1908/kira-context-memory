"""Sanitized authentication errors safe to map at the HTTP boundary."""


class AuthError(Exception):
    """Base class without credential or provider details."""


class InvalidCredentialsError(AuthError):
    """Username/password authentication was rejected."""


class InvalidSessionError(AuthError):
    """Opaque session is absent, expired, revoked or disabled."""


class InvalidCsrfTokenError(AuthError):
    """The request did not prove possession of the session-bound CSRF token."""


class PasswordPolicyError(AuthError):
    """A proposed password violates the application policy."""


class AuthStoreError(AuthError):
    """Authentication persistence failed without exposing database details."""


class AuthConflictError(AuthError):
    """A unique account or session identity already exists."""
