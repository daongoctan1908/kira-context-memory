"""Sanitized long-term-memory failures exposed through the port."""


class LongTermMemoryError(Exception):
    """Base error for long-term-memory operations."""


class LongTermMemoryConfigurationError(LongTermMemoryError):
    def __init__(self) -> None:
        super().__init__("Long-term memory configuration is invalid")


class LongTermMemoryConnectionError(LongTermMemoryError):
    def __init__(self) -> None:
        super().__init__("Long-term memory dependency is unavailable")


class LongTermMemoryTimeoutError(LongTermMemoryError):
    def __init__(self) -> None:
        super().__init__("Long-term memory operation timed out")


class LongTermMemoryOperationError(LongTermMemoryError):
    def __init__(self) -> None:
        super().__init__("Long-term memory operation failed")


class LongTermMemoryProtocolError(LongTermMemoryError):
    def __init__(self) -> None:
        super().__init__("Long-term memory returned invalid data")
