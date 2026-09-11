"""Safe typed failure boundary; raw provider/DB exceptions never become artifacts."""

from evaluation.models import Outcome, Reason


class PreflightError(Exception):
    def __init__(
        self,
        outcome: Outcome,
        reason: Reason,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.outcome = outcome
        self.reason = reason
        self.http_status = http_status


class ProtocolError(PreflightError):
    def __init__(self, reason: Reason) -> None:
        super().__init__(Outcome.PROTOCOL_ERROR, reason)
