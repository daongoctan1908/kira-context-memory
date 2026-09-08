"""Sanitized failures exposed by the asynchronous memory-job queue port."""


class MemoryJobQueueError(Exception):
    """Base error for durable memory-job queue operations."""


class MemoryJobQueueConfigurationError(MemoryJobQueueError):
    def __init__(self) -> None:
        super().__init__("Memory job queue configuration is invalid")


class MemoryJobQueueConnectionError(MemoryJobQueueError):
    def __init__(self) -> None:
        super().__init__("Memory job queue is unavailable")


class MemoryJobQueueOperationError(MemoryJobQueueError):
    def __init__(self) -> None:
        super().__init__("Memory job queue operation failed")


class MemoryJobQueueProtocolError(MemoryJobQueueError):
    def __init__(self) -> None:
        super().__init__("Memory job queue returned invalid data")


class MemoryJobLeaseLostError(MemoryJobQueueOperationError):
    def __init__(self) -> None:
        MemoryJobQueueError.__init__(self, "Memory job lease is no longer owned")
