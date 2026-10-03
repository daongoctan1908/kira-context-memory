"""Bounded OpenAI-compatible protocol probes with fixed synthetic inputs."""

import json
import math
import re
from hashlib import sha256
from uuid import uuid4

import httpx
from pydantic import ValidationError

from evaluation.config import EvalConfig, ProviderConfig
from evaluation.errors import PreflightError, ProtocolError
from evaluation.models import Outcome, Reason, TokenUsage
from evaluation.scoring import classify_scope

KIRA_READINESS_INPUT = "Kiểm tra kết nối: hãy trả lời một câu ngắn xác nhận đã nhận tin nhắn."


class _BoundedResponseStream(httpx.AsyncByteStream):
    """Bound authentication bodies and SSE lines before the native parser allocates them."""

    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self):
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > self._limit:
                raise ProtocolError(Reason.RESPONSE_TOO_LARGE)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


EXTRACTION_SYSTEM = (
    'Return only JSON in the native Mem0 V3 envelope: {"memory":['
    '{"id":"0","text":"a reusable fact","attributed_to":"user","scope":"GLOBAL"}]} . '
    "Each memory needs a sequential string id, nonempty text, and attributed_to "
    "equal to user or assistant, and scope equal to CONVERSATION or GLOBAL. "
    "No additional top-level keys. Extract the explicit standing preference as GLOBAL."
)
EXTRACTION_INPUT = "Dữ liệu giả lập: Tôi muốn báo cáo được trình bày dưới dạng bảng."
REWRITE_SYSTEM = "Rewrite the input as a standalone query in Vietnamese. Return only query text."
REWRITE_INPUT = "Dữ liệu giả lập: So sánh doanh thu tháng 1 và tháng 2 tại khu vực A."
EMBEDDING_INPUT = ["kira synthetic embedding probe one", "kira synthetic embedding probe two"]
JUDGE_SYSTEM = (
    "Classify whether the synthetic candidate satisfies the expectation. "
    "Return only JSON matching the supplied schema."
)
JUDGE_INPUT = {
    "candidate": "Hà Nội",
    "expectation": "The answer identifies Hà Nội.",
}
JUDGE_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason_code"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "UNCERTAIN"]},
        "reason_code": {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"},
    },
}


def api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return base + ("" if base.endswith("/v1") else "/v1") + path


def parse_json(value: str | bytes) -> object:
    def invalid_constant(_: str) -> None:
        raise ValueError("nonfinite JSON number")

    try:
        return json.loads(value, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError(Reason.INVALID_JSON) from None


def safe_model(value: object) -> str | None:
    if (
        isinstance(value, str)
        and not value.startswith("sk-")
        and re.fullmatch(r"[a-zA-Z0-9/][a-zA-Z0-9_./:-]{0,199}", value)
    ):
        return value
    return None


def observations(body: dict) -> dict:
    usage = body.get("usage")
    try:
        parsed_usage = (
            None
            if usage is None
            else TokenUsage.model_validate(
                {k: v for k, v in usage.items() if k in TokenUsage.model_fields}
            )
        )
    except (AttributeError, ValidationError):
        raise ProtocolError(Reason.INVALID_JSON) from None
    return {"returned_model": safe_model(body.get("model")), "usage": parsed_usage}


def chat_text(body: object) -> tuple[str, dict]:
    try:
        if not isinstance(body, dict):
            raise ValueError
        choices = body["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        text = message["content"]
        if (
            choice.get("finish_reason") != "stop"
            or message.get("role") != "assistant"
            or message.get("tool_calls")
            or message.get("function_call")
            or message.get("refusal")
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 16384
        ):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProtocolError(Reason.INVALID_CHAT) from None
    return text.strip(), observations(body)


def extraction_count(content: str) -> int:
    payload = parse_json(content)
    if not isinstance(payload, dict) or set(payload) != {"memory"}:
        raise ProtocolError(Reason.INVALID_EXTRACTION)
    memories = payload["memory"]
    if not isinstance(memories, list) or not memories or len(memories) > 10:
        # This fixed positive probe must produce a fact. Empty is not evidence of capability.
        raise ProtocolError(Reason.INVALID_EXTRACTION)
    for index, memory in enumerate(memories):
        if (
            not isinstance(memory, dict)
            or set(memory) - {"id", "text", "attributed_to", "linked_memory_ids", "scope"}
            or memory.get("id") != str(index)
            or memory.get("attributed_to") not in ("user", "assistant")
            or classify_scope(memory.get("scope")) not in ("CONVERSATION", "GLOBAL")
            or not isinstance(memory.get("text"), str)
            or not memory["text"].strip()
            or not isinstance(memory.get("linked_memory_ids", []), list)
            or any(not isinstance(v, str) for v in memory.get("linked_memory_ids", []))
        ):
            raise ProtocolError(Reason.INVALID_EXTRACTION)
    return len(memories)


def judge_observations(content: str) -> dict:
    payload = parse_json(content)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"verdict", "reason_code"}
        or payload["verdict"] not in {"PASS", "FAIL", "UNCERTAIN"}
        or not isinstance(payload["reason_code"], str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", payload["reason_code"])
    ):
        raise ProtocolError(Reason.INVALID_JUDGE)
    return {"judge_verdict": payload["verdict"]}


def embedding_observations(body: object, expected_dimension: int | None) -> dict:
    try:
        if not isinstance(body, dict):
            raise ValueError
        data = body["data"]
        if not isinstance(data, list) or len(data) != len(EMBEDDING_INPUT):
            raise ValueError
        seen: set[int] = set()
        dimensions: set[int] = set()
        for item in data:
            index = item["index"]
            vector = item["embedding"]
            if (
                type(index) is not int
                or index not in range(len(EMBEDDING_INPUT))
                or index in seen
                or not isinstance(vector, list)
                or not vector
                or len(vector) > 65536
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                or not any(vector)
            ):
                raise ValueError
            seen.add(index)
            dimensions.add(len(vector))
        if len(dimensions) != 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolError(Reason.INVALID_EMBEDDING) from None
    dimension = dimensions.pop()
    if expected_dimension is not None and dimension != expected_dimension:
        raise ProtocolError(Reason.DIMENSION_MISMATCH)
    return {"embedding_dimension": dimension, "embedding_count": len(data), **observations(body)}


class ProviderProbes:
    def __init__(self, client: httpx.AsyncClient, config: EvalConfig) -> None:
        self.client = client
        self.config = config

    async def request(
        self,
        method: str,
        url: str,
        *,
        provider: ProviderConfig | None = None,
        body: dict | None = None,
    ) -> object:
        headers = {"Content-Type": "application/json"}
        if provider and provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key.get_secret_value()}"
        timeout = httpx.Timeout(
            self.config.read_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
            write=self.config.connect_timeout_seconds,
            pool=self.config.connect_timeout_seconds,
        )
        try:
            async with self.client.stream(
                method,
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    status = response.status_code
                    reason = (
                        Reason.AUTH
                        if status in (401, 403)
                        else (Reason.RATE_LIMIT if status == 429 else Reason.HTTP_STATUS)
                    )
                    raise PreflightError(Outcome.DEPENDENCY_ERROR, reason, http_status=status)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > self.config.max_response_bytes:
                        raise ProtocolError(Reason.RESPONSE_TOO_LARGE)
                    chunks.extend(chunk)
        except httpx.TimeoutException:
            raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.TIMEOUT) from None
        except httpx.RequestError:
            raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.CONNECTION) from None
        return parse_json(bytes(chunks))

    async def chat(self, *, extraction: bool) -> dict:
        config = self.config
        provider = config.extraction if extraction else config.rewrite
        body = {
            "model": provider.model,
            "stream": False,
            "temperature": config.temperature,
            "max_tokens": config.extraction_max_tokens if extraction else config.rewrite_max_tokens,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM if extraction else REWRITE_SYSTEM},
                {"role": "user", "content": EXTRACTION_INPUT if extraction else REWRITE_INPUT},
            ],
        }
        if extraction and config.extraction_json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        response = await self.request(
            "POST",
            api_url(str(provider.base_url), "/chat/completions"),
            provider=provider,
            body=body,
        )
        content, details = chat_text(response)
        self._redact_returned_model(provider, details)
        if extraction:
            details["extracted_fact_count"] = extraction_count(content)
        return {"requested_model": provider.model, **details}

    async def embeddings(self) -> dict:
        provider = self.config.embedding
        body = {"model": provider.model, "input": EMBEDDING_INPUT, "encoding_format": "float"}
        if self.config.embedding_dimensions is not None:
            body["dimensions"] = self.config.embedding_dimensions
        response = await self.request(
            "POST",
            api_url(str(provider.base_url), "/embeddings"),
            provider=provider,
            body=body,
        )
        details = {
            "requested_model": provider.model,
            **embedding_observations(response, self.config.embedding_dimensions),
        }
        self._redact_returned_model(provider, details)
        return details

    async def judge(self) -> dict:
        provider = self.config.judge
        body = {
            "model": provider.model,
            "stream": False,
            "temperature": 0,
            "max_tokens": self.config.judge_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "week5_judge_preflight",
                    "strict": True,
                    "schema": JUDGE_RESPONSE_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(JUDGE_INPUT, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        response = await self.request(
            "POST",
            api_url(str(provider.base_url), "/chat/completions"),
            provider=provider,
            body=body,
        )
        content, details = chat_text(response)
        self._redact_returned_model(provider, details)
        return {"requested_model": provider.model, **details, **judge_observations(content)}

    @staticmethod
    def _redact_returned_model(provider: ProviderConfig, details: dict) -> None:
        model = details.get("returned_model")
        key = provider.api_key.get_secret_value() if provider.api_key else None
        if key and isinstance(model, str) and key in model:
            details["returned_model"] = None

    async def health(self, url: str) -> dict:
        response = await self.request("GET", url.rstrip("/") + "/ready")
        if not isinstance(response, dict) or response.get("status") != "ready":
            raise ProtocolError(Reason.INVALID_HEALTH)
        return {}

    async def kira_mock(self, url: str) -> dict:
        response = await self.request("GET", url.rstrip("/") + "/_test/requests")
        if not isinstance(response, dict) or response.get("stub") != "kira-synthetic-local-only":
            raise ProtocolError(Reason.INVALID_HEALTH)
        return {}

    async def kira_real(self) -> dict:
        """Authenticate and exhaust one synthetic chat on a fresh approved identity."""

        from app.config.settings import Settings
        from app.domain.errors.kira import (
            KiraAuthenticationError,
            KiraConnectionError,
            KiraHttpError,
            KiraProtocolError,
            KiraTimeoutError,
        )
        from app.infrastructure.kira.http_kira_client import KiraHttpAdapter

        config = self.config
        identity = f"benchmark_preflight_{uuid4().hex}"
        # EvalConfig validates the explicit values; construct without loading app .env.
        settings = Settings.model_construct(
            kira_base_url=config.kira_base_url,
            kira_username=identity,
            kira_domain=config.kira_domain,
            kira_basic_auth=config.kira_basic_auth,
            kira_service_id=config.kira_service_id,
            kira_device=config.kira_device,
            kira_message_type=config.kira_message_type,
            kira_connect_timeout_seconds=config.connect_timeout_seconds,
            kira_read_timeout_seconds=config.read_timeout_seconds,
            otel_enabled=False,
            otel_capture_content_enabled=False,
        )

        async def bound_response(response: httpx.Response) -> None:
            if response.is_stream_consumed:
                if len(response.content) > config.max_response_bytes:
                    raise ProtocolError(Reason.RESPONSE_TOO_LARGE)
            else:
                response.stream = _BoundedResponseStream(response.stream, config.max_response_bytes)

        hooks = self.client.event_hooks["response"]
        hooks.append(bound_response)
        stream = None
        try:
            adapter = KiraHttpAdapter(self.client, settings)
            stream = await adapter.chat_stream(KIRA_READINESS_INPUT)
            digest = sha256()
            text_bytes = 0
            events = 0
            has_text = False
            async for event in stream:
                events += 1
                if event.text_fragment:
                    fragment = event.text_fragment.encode("utf-8")
                    text_bytes += len(fragment)
                    digest.update(fragment)
                    has_text = has_text or bool(event.text_fragment.strip())
            if not events or not has_text:
                raise ProtocolError(Reason.INVALID_CHAT)
            return {
                "kira_response_sha256": digest.hexdigest(),
                "kira_event_count": events,
                "kira_text_bytes": text_bytes,
                "kira_context_isolation": "unique_username",
                "kira_identity_sha256": sha256(identity.encode()).hexdigest(),
            }
        except KiraAuthenticationError:
            raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.AUTH) from None
        except KiraHttpError as error:
            reason = Reason.AUTH if error.status_code in (401, 403) else Reason.HTTP_STATUS
            raise PreflightError(
                Outcome.DEPENDENCY_ERROR, reason, http_status=error.status_code
            ) from None
        except KiraTimeoutError:
            raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.TIMEOUT) from None
        except KiraConnectionError:
            raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.CONNECTION) from None
        except (KiraProtocolError, UnicodeError):
            raise ProtocolError(Reason.INVALID_CHAT) from None
        finally:
            if stream is not None:
                await stream.aclose()
            hooks.remove(bound_response)
