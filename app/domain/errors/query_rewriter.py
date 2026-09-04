"""Sanitized failures exposed by the query-rewriter port."""


class QueryRewriterError(Exception):
    """Base error for a failed rewrite operation."""


class QueryRewriterConfigurationError(QueryRewriterError):
    def __init__(self) -> None:
        super().__init__("Query rewriter configuration is invalid")


class QueryRewriterTimeoutError(QueryRewriterError):
    def __init__(self) -> None:
        super().__init__("Query rewriter timed out")


class QueryRewriterConnectionError(QueryRewriterError):
    def __init__(self) -> None:
        super().__init__("Query rewriter is unavailable")


class QueryRewriterHttpError(QueryRewriterError):
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Query rewriter returned HTTP {status_code}")


class QueryRewriterProtocolError(QueryRewriterError):
    def __init__(self) -> None:
        super().__init__("Query rewriter returned invalid output")
