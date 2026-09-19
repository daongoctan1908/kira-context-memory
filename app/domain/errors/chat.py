"""Application-level chat admission failures."""


class ChatAdmissionError(Exception):
    """Base class for a rejected product chat before downstream work starts."""


class ChatRateLimitExceededError(ChatAdmissionError):
    def __init__(self) -> None:
        super().__init__("Chat rate limit exceeded")


class ChatConcurrencyLimitError(ChatAdmissionError):
    def __init__(self) -> None:
        super().__init__("Chat concurrency limit exceeded")
