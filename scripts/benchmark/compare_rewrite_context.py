"""Paired, write-free rewrite/window comparison with a frozen HEAD control.

Run with python -m scripts.benchmark.compare_rewrite_context --env-file ... --output ...
The fixture is a fixed retrieval snapshot. This runner never queries or writes a memory DB.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.application.services.context_builder import ContextBuilder
from app.application.services.memory_retriever import ScopedMemoryRetriever
from app.application.services.rewrite_prompt import (
    REWRITE_PROMPT_VERSION,
    REWRITE_SYSTEM_PROMPT,
    build_rewrite_messages,
)
from app.config.settings import Settings
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import LongTermMemory
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from evaluation.config import EvalConfig, load_config
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.models import Profile, Suite
from evaluation.scoring import JudgeVerdict, score_constraints

ARMS = {"CONTROL": 10, "NEW_10": 10, "NEW_6": 6, "NEW_4": 4}
DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "tests/support/context_window_cases.json"


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("contract") != "context-window-tuning-v1":
        raise ValueError("unsupported context comparison fixture")
    digest = hashlib.sha256(fixture["control_system_prompt"].encode()).hexdigest()
    if digest != fixture.get("control_system_sha256"):
        raise ValueError("frozen control prompt checksum mismatch")
    cases = fixture["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("comparison cases must have unique identifiers")
    for case in cases:
        recent = case["recent_messages"]
        if len(recent) > 10 or len(recent) % 2:
            raise ValueError("fixture history must contain at most five complete turns")
        for index in range(0, len(recent), 2):
            user, assistant = recent[index : index + 2]
            if (
                user["role"] != "user"
                or assistant["role"] != "assistant"
                or user["turn_id"] != assistant["turn_id"]
            ):
                raise ValueError("fixture history contains an invalid turn")
            for message in (user, assistant):
                stamp = datetime.fromisoformat(message["timestamp"])
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError("fixture source timestamp must be timezone-aware")
        for branch, scope in (
            ("conversation_memories", "CONVERSATION"),
            ("global_memories", "GLOBAL"),
        ):
            records = case[branch]
            if len(records) > 10 or any(record["scope"] != scope for record in records):
                raise ValueError("fixture retrieval branch violates its scope or native cap")
    return fixture


def _memories(records: list[dict[str, Any]]) -> tuple[LongTermMemory, ...]:
    return tuple(
        LongTermMemory(
            memory_id=record["id"],
            content=record["text"],
            score=record["score"],
            metadata={
                "memory_scope": record["scope"],
                "source_timestamp": record["source_timestamp"],
            },
        )
        for record in records
    )


def _control_merge(local, global_memories) -> tuple[LongTermMemory, ...]:
    """Frozen 34fae5b behavior: local-first ID/content dedup, score ranking, cap ten."""
    ids: set[str] = set()
    texts: set[str] = set()
    merged: list[LongTermMemory] = []
    for memory in (*local, *global_memories):
        text = " ".join(memory.content.casefold().split())
        if memory.memory_id in ids or text in texts:
            continue
        ids.add(memory.memory_id)
        texts.add(text)
        merged.append(memory)
    return tuple(sorted(merged, key=lambda memory: (-memory.score, memory.memory_id))[:10])


def build_arm(fixture: dict[str, Any], case: dict[str, Any], arm: str):
    local = _memories(case["conversation_memories"])
    global_memories = _memories(case["global_memories"])
    memories = (
        _control_merge(local, global_memories)
        if arm == "CONTROL"
        else ScopedMemoryRetriever._merge(local, global_memories, 10)
    )
    recent = tuple(
        ConversationMessage(
            session_id=case["id"],
            turn_id=message["turn_id"],
            role=ConversationRole(message["role"]),
            content=message["content"],
            timestamp=datetime.fromisoformat(message["timestamp"]),
        )
        for message in case["recent_messages"]
    )
    context = ContextBuilder(max_recent_messages=ARMS[arm], recent_token_budget=3000).build(
        recent, case["query"], memories
    )
    if arm == "CONTROL":
        envelope = {
            "long_term_memories": [memory.content for memory in context.long_term_memories],
            "recent_messages": [
                {"role": message.role.value, "content": message.content}
                for message in context.recent_messages
            ],
            "current_query": context.current_query,
        }
        messages = [
            {"role": "system", "content": fixture["control_system_prompt"]},
            {
                "role": "user",
                "content": json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            },
        ]
    else:
        messages = build_rewrite_messages(context)
    return context, messages


class ComparisonTransport(httpx.AsyncBaseTransport):
    """Inject an arm's prepared prompt into the native adapter's otherwise unchanged request.

    The runner makes calls sequentially. One shared underlying transport preserves connection
    pooling; arm selection never changes the application prompt builder or model settings.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.messages: list[dict[str, str]] | None = None
        self.input_tokens: int | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.messages is None:
            raise RuntimeError("comparison prompt was not bound")
        payload = json.loads(request.content)
        payload["messages"] = self.messages
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers={
                key: value for key, value in request.headers.items() if key != "content-length"
            },
            content=json.dumps(payload, ensure_ascii=False).encode(),
            extensions=request.extensions,
        )
        response = await self.inner.handle_async_request(forwarded)
        await response.aread()
        if response.is_success:
            try:
                usage = response.json().get("usage", {})
                value = usage.get("prompt_tokens", usage.get("input_tokens"))
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.input_tokens = value
            except (ValueError, AttributeError):
                pass
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)] if values else 0.0


def summarize(fixture, results: list[dict[str, Any]], repetitions: int) -> dict[str, Any]:
    expected_keys = {
        (case["id"], arm, repeat)
        for case in fixture["cases"]
        for arm in ARMS
        for repeat in range(repetitions)
    }
    keyed = {}
    invalid_result_rows = 0
    for row in results:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("case_id"), str)
            or not isinstance(row.get("arm"), str)
            or type(row.get("repeat")) is not int
        ):
            invalid_result_rows += 1
            continue
        key = (row["case_id"], row["arm"], row["repeat"])
        if key not in expected_keys or key in keyed:
            invalid_result_rows += 1
            continue
        keyed[key] = row
    complete = invalid_result_rows == 0 and set(keyed) == expected_keys
    eligible: list[str] = []
    for arm in ("NEW_4", "NEW_6", "NEW_10"):
        if not complete:
            continue
        safe = True
        smaller = False
        for case in fixture["cases"]:
            for repeat in range(repetitions):
                candidate = keyed[(case["id"], arm, repeat)]
                control = keyed[(case["id"], "CONTROL", repeat)]
                new10 = keyed[(case["id"], "NEW_10", repeat)]
                if candidate["outcome"] != "pass":
                    safe = False
                if control["outcome"] == "pass" or new10["outcome"] == "pass":
                    safe = safe and candidate["outcome"] == "pass"
                if arm != "NEW_10":
                    if candidate["input_tokens"] is not None and new10["input_tokens"] is not None:
                        smaller |= candidate["input_tokens"] < new10["input_tokens"]
                        safe &= candidate["input_tokens"] <= new10["input_tokens"]
                    else:
                        # Bytes are reported as a proxy, never as proof of fewer model tokens.
                        safe = False
        if safe and (arm == "NEW_10" or smaller):
            eligible.append(arm)
    arm_summary = {}
    for arm in ARMS:
        rows = [row for row in keyed.values() if row["arm"] == arm]
        latency = [row["latency_ms"] for row in rows]
        input_tokens = [row["input_tokens"] for row in rows if row["input_tokens"] is not None]
        all_input_tokens = bool(rows) and len(input_tokens) == len(rows)
        arm_summary[arm] = {
            "passed": sum(row["outcome"] == "pass" for row in rows),
            "trials": len(rows),
            "errors": sum(row["outcome"] == "dependency_error" for row in rows),
            "review_required": sum(row["outcome"] == "review_required" for row in rows),
            "median_prompt_bytes": statistics.median(row["prompt_bytes"] for row in rows)
            if rows
            else 0,
            "input_tokens_available": all_input_tokens,
            "input_tokens_samples": len(input_tokens),
            "input_tokens_median": statistics.median(input_tokens) if input_tokens else None,
            "input_tokens_total": sum(input_tokens) if all_input_tokens else None,
            "latency_p50_ms": statistics.median(latency) if latency else 0,
            "latency_p95_ms": _p95(latency),
        }
    selected = min(
        eligible,
        key=lambda arm: (
            arm_summary[arm]["input_tokens_total"]
            if arm_summary[arm]["input_tokens_total"] is not None
            else math.inf,
            -ARMS[arm],
        ),
        default=None,
    )
    return {
        "complete": complete,
        "invalid_result_rows": invalid_result_rows,
        "contract_quality_passed": "NEW_10" in eligible,
        "quality_and_size_eligible": eligible,
        "candidate_max_recent_messages": ARMS[selected] if selected is not None else 10,
        "deployment_max_recent_messages": 10,
        "promotion": "manual_latency_review_required" if eligible else "keep_control_window",
        "arms": arm_summary,
    }


async def compare(
    fixture: dict[str, Any],
    config: EvalConfig,
    *,
    repetitions: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
    judge=None,
) -> dict[str, Any]:
    if not config.rewrite.configured or repetitions < 1:
        raise ValueError("comparison requires configured rewrite provider and positive repetitions")
    # EvalConfig has already validated these fields. Do not load ambient gateway settings
    # or require unrelated KiRa credentials for a write-free rewrite comparison.
    settings = Settings.model_construct(
        vllm_base_url=config.rewrite.base_url,
        vllm_model=config.rewrite.model,
        vllm_api_key=config.rewrite.api_key,
        vllm_connect_timeout_seconds=config.connect_timeout_seconds,
        vllm_read_timeout_seconds=config.read_timeout_seconds,
    )
    injected = ComparisonTransport(transport or httpx.AsyncHTTPTransport())
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=injected) as client, httpx.AsyncClient() as judge_client:
        rewriter = VllmQueryRewriterAdapter(client, settings)
        semantic_judge = judge or InternalSemanticJudge(judge_client, config)
        for repeat in range(repetitions):
            names = list(ARMS)
            names = names[repeat % len(names) :] + names[: repeat % len(names)]
            for case in fixture["cases"]:
                for arm in names:
                    context, messages = build_arm(fixture, case, arm)
                    injected.messages = messages
                    injected.input_tokens = None
                    started = time.perf_counter()
                    latency = None
                    output = None
                    reason = None
                    judge_rationale = None
                    try:
                        output = await rewriter.rewrite(context)
                        latency = (time.perf_counter() - started) * 1000
                        score = score_constraints(
                            output,
                            required_exact=case["required_exact"],
                            forbidden=case["forbidden"],
                        )
                        if not score.passed:
                            outcome, reason = "fail", "deterministic_constraints"
                        else:
                            judgment = await semantic_judge.semantic(
                                case_id=case["id"],
                                suite=Suite.REWRITE,
                                output=output,
                                semantic_expectation=case["semantic_expectation"],
                                required_exact=case["required_exact"],
                                forbidden=case["forbidden"],
                            )
                            outcome = {
                                JudgeVerdict.PASS: "pass",
                                JudgeVerdict.FAIL: "fail",
                                JudgeVerdict.UNCERTAIN: "review_required",
                            }[judgment.verdict]
                            reason = judgment.reason_code
                            judge_rationale = getattr(judgment, "rationale", None)
                    except JudgeError as error:
                        outcome, reason = "review_required", error.reason_code
                    except Exception as error:
                        if latency is None:
                            latency = (time.perf_counter() - started) * 1000
                        outcome, reason = "dependency_error", type(error).__name__
                    finally:
                        injected.messages = None
                    results.append(
                        {
                            "case_id": case["id"],
                            "arm": arm,
                            "repeat": repeat,
                            "outcome": outcome,
                            "reason": reason,
                            "judge_rationale": judge_rationale,
                            "output": output,
                            "latency_ms": round(latency, 2),
                            "input_tokens": injected.input_tokens,
                            "recent_retained_messages": len(context.recent_messages),
                            "recent_retained_turns": len(context.recent_messages) // 2,
                            "trim_reason": context.trim_reason,
                            "estimated_recent_tokens": context.estimated_recent_tokens,
                            "prompt_bytes": sum(
                                len(message["content"].encode()) for message in messages
                            ),
                            "prompt_sha256": hashlib.sha256(
                                json.dumps(
                                    messages, ensure_ascii=False, separators=(",", ":")
                                ).encode()
                            ).hexdigest(),
                        }
                    )
    fixture_hash = hashlib.sha256(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "contract": fixture["contract"],
        "source_head": fixture["head"],
        "fixture_sha256": fixture_hash,
        "config_sha256": config.fingerprint(),
        "repetitions": repetitions,
        "recent_token_budget": 3000,
        "candidate_prompt_version": REWRITE_PROMPT_VERSION,
        "control_prompt_version": fixture["control_prompt_version"],
        "candidate_system_prompt_sha256": hashlib.sha256(
            REWRITE_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "control_system_prompt_sha256": hashlib.sha256(
            fixture["control_system_prompt"].encode()
        ).hexdigest(),
        "summary": summarize(fixture, results, repetitions),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--profile", choices=("internal_test", "pc_openai_acceptance"), default="internal_test"
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(
        profile=Profile(args.profile),
        suites=(Suite.REWRITE,),
        env_file=args.env_file,
        environment={},
    )
    report = asyncio.run(compare(load_fixture(args.fixture), config, repetitions=args.repetitions))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["contract_quality_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
