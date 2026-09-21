"""Opt-in, write-free memory-policy gate against an OpenAI-compatible memory LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT, generate_additive_extraction_prompt
from mem0.memory.utils import extract_json, parse_messages, remove_code_blocks

from app.application.services.memory_policy import (
    MEMORY_EXTRACTION_INSTRUCTIONS,
    MEMORY_POLICY_VERSION,
    MEMORY_TAXONOMY,
)
from tests.support.memory_policy_cases import (
    CASES,
    MEMORY_POLICY_EVAL_VERSION,
    MemoryPolicyCase,
)


class PolicyEvalProtocolError(Exception):
    """The model returned a response that Mem0 cannot consume safely."""


@dataclass(frozen=True, slots=True)
class PolicyEvalOptions:
    base_url: str
    model: str
    timeout_seconds: float = 30.0
    max_tokens: int = 1000
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            parsed_url = httpx.URL(self.base_url)
        except ValueError as error:
            raise ValueError("memory policy eval base URL is invalid") from error
        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.host
            or not self.model.strip()
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ValueError("memory policy eval requires base URL, model, and positive timeout")


@dataclass(frozen=True, slots=True)
class PolicyEvalResult:
    case: str
    outcome: str
    reason_codes: tuple[str, ...]
    fact_count: int
    latency_ms: int
    policy_version: str = MEMORY_POLICY_VERSION
    eval_version: str = MEMORY_POLICY_EVAL_VERSION

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"


def build_extraction_messages(case: MemoryPolicyCase) -> list[dict[str, str]]:
    """Build the exact V3 extraction prompt shape without writing a memory."""
    parsed_messages = parse_messages(
        [{"role": message.role, "content": message.content} for message in case.messages]
    )
    user_prompt = generate_additive_extraction_prompt(
        existing_memories=[
            {"id": str(index), "text": text} for index, text in enumerate(case.existing_memories)
        ],
        new_messages=parsed_messages,
        last_k_messages=[],
        current_date=case.observation_date,
        timestamp=case.observation_date,
        custom_instructions=MEMORY_EXTRACTION_INSTRUCTIONS,
    )
    return [
        {"role": "system", "content": ADDITIVE_EXTRACTION_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def parse_memory_facts(content: object) -> tuple[str, ...]:
    """Parse the same JSON memory envelope consumed by Mem0's V3 pipeline."""
    cleaned = remove_code_blocks(content)
    if not cleaned:
        raise PolicyEvalProtocolError
    try:
        payload = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        try:
            payload = json.loads(extract_json(cleaned), strict=False)
        except (TypeError, ValueError) as error:
            raise PolicyEvalProtocolError from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("memory"), list):
        raise PolicyEvalProtocolError

    facts: list[str] = []
    for item in payload["memory"]:
        if not isinstance(item, Mapping):
            raise PolicyEvalProtocolError
        if set(item) - {"id", "text", "attributed_to", "linked_memory_ids"}:
            raise PolicyEvalProtocolError
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PolicyEvalProtocolError
        attributed_to = item.get("attributed_to")
        if attributed_to is not None and attributed_to not in ("user", "assistant"):
            raise PolicyEvalProtocolError
        facts.append(text.strip())
    return tuple(facts)


def score_case(
    case: MemoryPolicyCase, facts: Sequence[str], latency_ms: int = 0
) -> PolicyEvalResult:
    """Score facts without returning their potentially sensitive text in evidence."""
    expectation = case.expectation
    combined = "\n".join(facts)
    normalized = _normalize(combined)
    reasons: list[str] = []

    if not expectation.min_facts <= len(facts) <= expectation.max_facts:
        reasons.append("fact_count")
    for term in expectation.required_terms:
        if _normalize(term) not in normalized:
            reasons.append("missing_required_term")
    for fragment in expectation.required_exact_fragments:
        if fragment not in combined:
            reasons.append("missing_exact_fragment")
    for alternatives in expectation.required_any_terms:
        if not any(_normalize(term) in normalized for term in alternatives):
            reasons.append("missing_required_alternative")
    for group in expectation.required_fact_terms:
        if not any(all(_normalize(term) in _normalize(fact) for term in group) for fact in facts):
            reasons.append("missing_fact_context")
    for term in expectation.forbidden_terms:
        if _normalize(term) in normalized:
            reasons.append("forbidden_term")
    if any(_has_taxonomy_prefix(fact) for fact in facts):
        reasons.append("taxonomy_prefix")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return PolicyEvalResult(
        case=case.name,
        outcome="pass" if not unique_reasons else "fail",
        reason_codes=unique_reasons,
        fact_count=len(facts),
        latency_ms=max(0, latency_ms),
    )


class MemoryPolicyEvalClient:
    def __init__(self, client: httpx.AsyncClient, options: PolicyEvalOptions) -> None:
        self._client = client
        self._options = options

    async def evaluate(self, case: MemoryPolicyCase) -> PolicyEvalResult:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                _chat_completions_url(self._options.base_url),
                headers=_authorization_header(self._options.api_key),
                json={
                    "model": self._options.model,
                    "messages": build_extraction_messages(case),
                    "temperature": 0,
                    "max_tokens": self._options.max_tokens,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            facts = parse_memory_facts(_response_content(response))
        except (httpx.HTTPError, ValueError, PolicyEvalProtocolError) as error:
            return PolicyEvalResult(
                case=case.name,
                outcome="dependency_error",
                reason_codes=(type(error).__name__,),
                fact_count=0,
                latency_ms=_latency_ms(started),
            )
        return score_case(case, facts, _latency_ms(started))


async def evaluate_cases(
    options: PolicyEvalOptions,
    cases: Sequence[MemoryPolicyCase] = CASES,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[PolicyEvalResult, ...]:
    timeout = httpx.Timeout(
        options.timeout_seconds,
        connect=min(5.0, options.timeout_seconds),
    )
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        evaluator = MemoryPolicyEvalClient(client, options)
        results: list[PolicyEvalResult] = []
        for case in cases:
            result = await evaluator.evaluate(case)
            results.append(result)
            if result.outcome == "dependency_error":
                break
        return tuple(results)


def sanitized_report(results: Sequence[PolicyEvalResult]) -> dict[str, object]:
    return {
        "policy_version": MEMORY_POLICY_VERSION,
        "eval_version": MEMORY_POLICY_EVAL_VERSION,
        "total": len(results),
        "passed": sum(result.passed for result in results),
        "failed": sum(not result.passed for result in results),
        "cases": [asdict(result) for result in results],
    }


def options_from_environment(environ: Mapping[str, str] | None = None) -> PolicyEvalOptions:
    source = os.environ if environ is None else environ
    base_url = source.get("MEMORY_LLM_BASE_URL", "")
    model = source.get("MEMORY_LLM_MODEL", "")
    timeout = float(source.get("MEMORY_OPERATION_TIMEOUT_SECONDS", "30"))
    max_tokens = int(source.get("MEMORY_LLM_MAX_TOKENS", "1000"))
    return PolicyEvalOptions(
        base_url=base_url,
        model=model,
        timeout_seconds=timeout,
        max_tokens=max_tokens,
        api_key=source.get("MEMORY_LLM_API_KEY"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", dest="case_names")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


async def run(
    argv: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = parse_args(argv)
    try:
        selected = _select_cases(args.case_names)
        results = await evaluate_cases(
            options_from_environment(environ),
            selected,
            transport=transport,
        )
    except (TypeError, ValueError) as error:
        print(json.dumps({"outcome": "configuration_error", "error_class": type(error).__name__}))
        return 2

    report = sanitized_report(results)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("policy_version", "eval_version", "total", "passed", "failed")
            },
            sort_keys=True,
        )
    )
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            print(json.dumps({"outcome": "report_error", "error_class": type(error).__name__}))
            return 2
    return int(report["failed"] != 0)


def main() -> int:
    return asyncio.run(run())


def _response_content(response: httpx.Response) -> object:
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise PolicyEvalProtocolError
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise PolicyEvalProtocolError
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise PolicyEvalProtocolError
    content = message.get("content")
    if not isinstance(content, str):
        raise PolicyEvalProtocolError
    return content


def _select_cases(case_names: list[str] | None) -> tuple[MemoryPolicyCase, ...]:
    if not case_names:
        return CASES
    by_name = {case.name: case for case in CASES}
    unknown = set(case_names) - set(by_name)
    if unknown:
        raise ValueError("unknown memory policy case")
    return tuple(by_name[name] for name in case_names)


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized.endswith("/v1"):
        normalized += "/v1"
    return normalized + "/chat/completions"


def _authorization_header(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _has_taxonomy_prefix(fact: str) -> bool:
    normalized = _normalize(fact).lstrip("[( ")
    normalized = re.sub(r"[_-]+", " ", normalized)
    return any(
        normalized.startswith(_normalize(taxonomy).replace("_", " "))
        for taxonomy in MEMORY_TAXONOMY
    )


def _latency_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


if __name__ == "__main__":
    raise SystemExit(main())
