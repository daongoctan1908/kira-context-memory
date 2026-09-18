"""Fail-closed internal semantic judge for canonical benchmark outputs.

The judge is intentionally independent of telemetry and external-provider config. It receives
only the evaluated content and gold criteria, never the candidate/control variant identity.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Annotated, Literal

import httpx
from pydantic import Field, StringConstraints, ValidationError, model_validator

from evaluation.config import EvalConfig
from evaluation.models import EvalModel, Identifier, Outcome, Profile, Suite
from evaluation.providers import api_url
from evaluation.scoring import (
    FormationMatchDecision,
    FormationMatchVerdict,
    JudgeProvenance,
    JudgeVerdict,
    SemanticJudgment,
    output_sha256,
)

_SEMANTIC_SYSTEM_PROMPT = """You are an impartial evaluator for an internal Vietnamese chatbot.
Judge only whether the candidate output satisfies the supplied semantic expectation and explicit
constraints. Do not reward style, infer missing evidence, or use outside knowledge. PASS means the
intent and required constraints are preserved without unsupported additions. FAIL means a material
requirement is contradicted, omitted, or invented. UNCERTAIN means the supplied evidence is not
enough to decide. Return only JSON matching the response schema."""

_FORMATION_SYSTEM_PROMPT = """You are an impartial evaluator of atomic memory facts. Match each
candidate prediction to at most one semantically equivalent gold fact. A gold fact may be used at
most once. Numeric thresholds, operators, units, entities, attribution, and time scope must agree.
Use NO_MATCH for unsupported or materially different facts and UNCERTAIN when the supplied text is
insufficient to decide. Return one decision for every requested prediction and only JSON matching
the response schema."""

_Rationale = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class JudgeError(Exception):
    """Safe execution failure; provider response text and exception messages are discarded."""

    def __init__(
        self,
        outcome: Literal[Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR],
        reason_code: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.outcome = outcome
        self.reason_code = reason_code
        self.http_status = http_status


class _SemanticResponse(EvalModel):
    verdict: JudgeVerdict
    reason_code: Identifier
    rationale: _Rationale


class _FormationResponseItem(EvalModel):
    prediction_index: int = Field(ge=0, strict=True)
    verdict: FormationMatchVerdict
    gold_id: Identifier | None = None
    reason_code: Identifier

    @model_validator(mode="after")
    def match_requires_gold(self) -> "_FormationResponseItem":
        if (self.verdict is FormationMatchVerdict.MATCH) != (self.gold_id is not None):
            raise ValueError("only MATCH may identify a gold fact")
        return self


class _FormationResponse(EvalModel):
    decisions: tuple[_FormationResponseItem, ...] = Field(min_length=1)


def _schema_hash(model: type[EvalModel]) -> str:
    schema = json.dumps(
        model.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(schema.encode("utf-8")).hexdigest()


def _prompt_hash(prompt: str) -> str:
    return sha256(prompt.encode("utf-8")).hexdigest()


def _parse_json(value: str | bytes) -> object:
    def reject_nonfinite(_: str) -> None:
        raise ValueError("nonfinite JSON number")

    try:
        return json.loads(value, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_json") from None


class InternalSemanticJudge:
    """One-call, zero-retry client for an approved OpenAI-compatible judge."""

    def __init__(self, client: httpx.AsyncClient, config: EvalConfig) -> None:
        if config.profile not in {Profile.PC_OPENAI_ACCEPTANCE, Profile.INTERNAL_TEST}:
            raise ValueError("semantic judge requires an approved acceptance profile")
        if not config.judge.configured:
            raise ValueError("semantic judge requires an explicit endpoint and model")
        self._client = client
        self._config = config

    def _provenance(self, *, prompt: str, response_model: type[EvalModel]) -> JudgeProvenance:
        assert self._config.judge.model is not None
        return JudgeProvenance(
            profile=self._config.profile,
            provider=(
                "openai_external"
                if self._config.profile is Profile.PC_OPENAI_ACCEPTANCE
                else "internal_openai_compatible"
            ),
            model=self._config.judge.model,
            deployment=self._config.judge_deployment,
            prompt_sha256=_prompt_hash(prompt),
            response_schema_sha256=_schema_hash(response_model),
        )

    async def semantic(
        self,
        *,
        case_id: str,
        suite: Literal[Suite.REWRITE, Suite.CROSS_SESSION],
        output: object,
        semantic_expectation: str,
        reference_answer: object | None = None,
        required_exact: Sequence[str] = (),
        forbidden: Sequence[str] = (),
    ) -> SemanticJudgment:
        payload = {
            "candidate_output": output,
            "semantic_expectation": semantic_expectation,
            "reference_answer": reference_answer,
            "required_exact": list(required_exact),
            "forbidden": list(forbidden),
        }
        response = await self._chat(
            system_prompt=_SEMANTIC_SYSTEM_PROMPT,
            payload=payload,
            response_model=_SemanticResponse,
            schema_name="semantic_judgment",
        )
        assert isinstance(response, _SemanticResponse)
        return SemanticJudgment(
            case_id=case_id,
            suite=suite,
            output_sha256=output_sha256(output),
            verdict=response.verdict,
            reason_code=response.reason_code,
            rationale=response.rationale,
            judge=self._provenance(
                prompt=_SEMANTIC_SYSTEM_PROMPT,
                response_model=_SemanticResponse,
            ),
        )

    async def formation(
        self,
        *,
        predicted_facts: Sequence[str],
        gold_facts: Mapping[str, str],
        prediction_indexes: Sequence[int],
    ) -> tuple[FormationMatchDecision, ...]:
        indexes = tuple(prediction_indexes)
        if not indexes or len(indexes) != len(set(indexes)):
            raise ValueError("formation judge requires unique prediction indexes")
        if any(index < 0 or index >= len(predicted_facts) for index in indexes):
            raise ValueError("formation judge prediction index is out of range")
        payload = {
            "predictions": [
                {"prediction_index": index, "text": predicted_facts[index]} for index in indexes
            ],
            "gold_facts": [
                {"gold_id": gold_id, "text": text} for gold_id, text in gold_facts.items()
            ],
        }
        response = await self._chat(
            system_prompt=_FORMATION_SYSTEM_PROMPT,
            payload=payload,
            response_model=_FormationResponse,
            schema_name="formation_judgment",
        )
        assert isinstance(response, _FormationResponse)
        returned_indexes = [decision.prediction_index for decision in response.decisions]
        if len(returned_indexes) != len(set(returned_indexes)) or set(returned_indexes) != set(
            indexes
        ):
            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_prediction_set")
        matched_gold = [
            decision.gold_id
            for decision in response.decisions
            if decision.verdict is FormationMatchVerdict.MATCH
        ]
        if any(gold_id not in gold_facts for gold_id in matched_gold) or len(matched_gold) != len(
            set(matched_gold)
        ):
            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_gold_mapping")
        provenance = self._provenance(
            prompt=_FORMATION_SYSTEM_PROMPT,
            response_model=_FormationResponse,
        )
        return tuple(
            FormationMatchDecision(
                prediction_index=decision.prediction_index,
                verdict=decision.verdict,
                gold_id=decision.gold_id,
                reason_code=decision.reason_code,
                judge=provenance,
            )
            for decision in sorted(response.decisions, key=lambda item: item.prediction_index)
        )

    async def _chat(
        self,
        *,
        system_prompt: str,
        payload: object,
        response_model: type[EvalModel],
        schema_name: str,
    ) -> EvalModel:
        provider = self._config.judge
        assert provider.base_url is not None and provider.model is not None
        schema = response_model.model_json_schema()
        body = {
            "model": provider.model,
            "stream": False,
            "temperature": 0,
            "max_tokens": self._config.judge_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        response = await self._request(
            api_url(str(provider.base_url), "/chat/completions"),
            body,
        )
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            content = message["content"]
            if (
                choice.get("finish_reason") != "stop"
                or message.get("role") != "assistant"
                or message.get("tool_calls")
                or message.get("function_call")
                or message.get("refusal")
                or not isinstance(content, str)
                or not content.strip()
                or len(content) > 16384
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_chat") from None
        parsed = _parse_json(content)
        try:
            return response_model.model_validate(parsed)
        except ValidationError:
            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_schema") from None

    async def _request(self, url: str, body: dict) -> dict:
        provider = self._config.judge
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key.get_secret_value()}"
        timeout = httpx.Timeout(
            self._config.read_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
            write=self._config.connect_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )
        try:
            async with asyncio.timeout(self._config.total_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        status = response.status_code
                        reason = (
                            "judge_authentication"
                            if status in (401, 403)
                            else ("judge_rate_limit" if status == 429 else "judge_http_status")
                        )
                        raise JudgeError(
                            Outcome.DEPENDENCY_ERROR,
                            reason,
                            http_status=status,
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > self._config.max_response_bytes:
                            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_response_too_large")
                        chunks.extend(chunk)
        except TimeoutError:
            raise JudgeError(Outcome.DEPENDENCY_ERROR, "judge_timeout") from None
        except httpx.TimeoutException:
            raise JudgeError(Outcome.DEPENDENCY_ERROR, "judge_timeout") from None
        except httpx.RequestError:
            raise JudgeError(Outcome.DEPENDENCY_ERROR, "judge_connection") from None
        parsed = _parse_json(bytes(chunks))
        if not isinstance(parsed, dict):
            raise JudgeError(Outcome.PROTOCOL_ERROR, "judge_invalid_chat")
        return parsed
