"""HTTPX adapter for the vLLM OpenAI-compatible chat completions endpoint."""

import httpx

from app.application.services.rewrite_prompt import build_rewrite_messages
from app.config.settings import Settings
from app.domain.errors.query_rewriter import (
    QueryRewriterConfigurationError,
    QueryRewriterConnectionError,
    QueryRewriterHttpError,
    QueryRewriterProtocolError,
    QueryRewriterTimeoutError,
)
from app.domain.models.context import ConversationContext


class VllmQueryRewriterAdapter:
    """Rewrite through a caller-owned HTTP client; no retries or business answers."""

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        base_url = settings.vllm_base_url
        model = settings.vllm_model
        if (
            base_url is None
            or model is None
            or not model.strip()
            or base_url.username
            or base_url.password
            or base_url.query
            or base_url.fragment
        ):
            raise QueryRewriterConfigurationError

        base = str(base_url).rstrip("/")
        self._url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self._model = model.strip()
        self._client = http_client
        self._max_output_chars = settings.vllm_max_output_chars
        self._headers = {"Content-Type": "application/json"}
        if settings.vllm_api_key is not None:
            api_key = settings.vllm_api_key.get_secret_value()
            if not api_key.strip():
                raise QueryRewriterConfigurationError
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = httpx.Timeout(
            connect=settings.vllm_connect_timeout_seconds,
            read=settings.vllm_read_timeout_seconds,
            write=settings.vllm_connect_timeout_seconds,
            pool=settings.vllm_connect_timeout_seconds,
        )

    async def rewrite(self, context: ConversationContext) -> str:
        try:
            response = await self._client.post(
                self._url,
                json={
                    "model": self._model,
                    "messages": build_rewrite_messages(context),
                    "temperature": 0,
                    "stream": False,
                    "max_tokens": 256,
                },
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as error:
            raise QueryRewriterTimeoutError from error
        except httpx.RequestError as error:
            raise QueryRewriterConnectionError from error

        if not response.is_success:
            raise QueryRewriterHttpError(status_code=response.status_code)

        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            choices = body["choices"]
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError
            choice = choices[0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise ValueError
            message = choice["message"]
            if not isinstance(message, dict) or message.get("tool_calls"):
                raise ValueError
            content = message["content"]
            if not isinstance(content, str) or len(content) > self._max_output_chars:
                raise ValueError
            standalone_query = content.strip()
            if not standalone_query:
                raise ValueError
        except (ValueError, KeyError, TypeError) as error:
            raise QueryRewriterProtocolError from error

        return standalone_query
