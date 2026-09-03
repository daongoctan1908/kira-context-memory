"""Sanitized KiRa client failures shared across the port boundary."""


class KiraClientError(Exception):
    """Base error safe for application-level mapping.

    Messages and attributes must never contain Basic credentials or runtime tokens.
    """

    code = "KIRA_CLIENT_ERROR"
    retryable = False


class KiraAuthenticationError(KiraClientError):
    """KiRa rejected authentication or returned no usable token."""

    code = "KIRA_AUTH_FAILED"

    def __init__(self, *, downstream_code: str | None = None) -> None:
        super().__init__("KiRa authentication failed")
        self.downstream_code = downstream_code


class KiraConnectionError(KiraClientError):
    """The Gateway could not establish a connection to KiRa."""

    code = "KIRA_CONNECTION_ERROR"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Could not connect to KiRa")


class KiraTimeoutError(KiraClientError):
    """A KiRa request exceeded its configured timeout."""

    code = "KIRA_TIMEOUT"
    retryable = True

    def __init__(self, *, stage: str) -> None:
        super().__init__(f"KiRa {stage} timed out")
        self.stage = stage


class KiraHttpError(KiraClientError):
    """KiRa returned a non-success HTTP status."""

    code = "KIRA_HTTP_ERROR"

    def __init__(self, *, status_code: int) -> None:
        super().__init__(f"KiRa returned HTTP {status_code}")
        self.status_code = status_code
        self.retryable = status_code >= 500


class KiraProtocolError(KiraClientError):
    """KiRa returned a response that violates the confirmed baseline contract."""

    code = "KIRA_PROTOCOL_ERROR"

    def __init__(self, message: str = "KiRa returned an invalid response") -> None:
        super().__init__(message)


class KiraMalformedSseError(KiraProtocolError):
    """A KiRa ``data:`` frame did not contain valid JSON."""

    code = "KIRA_MALFORMED_SSE"

    def __init__(self) -> None:
        super().__init__("KiRa returned a malformed SSE data frame")
