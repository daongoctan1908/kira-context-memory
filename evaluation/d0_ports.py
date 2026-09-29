"""Production-faithful provider ports for the D0-local characterization.

Evaluation-only adapters over the repo's existing OpenAI-compatible protocol
patterns (evaluation/providers.py, evaluation/judge.py). The embedding adapter
mirrors the production embedder semantics of
packages/viettel-mem0/mem0/embeddings/openai.py: OpenAI-compatible POST /embeddings,
``encoding_format: float``, newline-only preprocessing (``text.replace("\n", " ")``),
optional ``dimensions`` parameter, batch <= 100, no normalization client-side.
The decision adapter mirrors evaluation/judge.py InternalSemanticJudge: temperature 0,
strict json_schema response format, streaming read with byte cap, zero retries,
fail-closed typed errors. No runtime code is modified or imported.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Literal

import httpx
from pydantic import ValidationError

from evaluation.config import EvalConfig, ProviderConfig
from evaluation.d0_local_executor import (
    CONFLICT_RESPONSE_SCHEMA,
    DEFAULT_DECODING_PARAMS as DEFAULT_DECODING_PARAMS_REF,
    DecisionPort,
    DecisionPortError,
    EmbeddingPort,
    InvalidDecision,
    _parse_decision,
)
from evaluation.providers import api_url
from evaluation.shadow_lifecycle import GoldEvent, ScheduleBundle, ShadowTimeline

EMBEDDING_BATCH_LIMIT = 100

D0_CONFLICT_PROMPT_VERSION = "d0-conflict-v1"
D0_CONFLICT_PROMPT_VERSION_V1 = "d0-conflict-v1"
D0_CONFLICT_PROMPT_VERSION_V2 = "d0-conflict-v2"

D0_CONFLICT_SYSTEM_PROMPT = """You are the conflict-resolution stage of a long-term
memory system for a Vietnamese chat assistant. A NEW candidate memory fact is about to
be stored. You receive it together with the existing ACTIVE memories that a semantic
search found as possible conflicts. Decide how the candidate relates to them.

Decisions:
- DUPLICATE: the candidate expresses the same current fact as exactly one existing
  memory (paraphrase, restatement, or added detail that does not change the fact).
  Keeping both rows would create a redundant duplicate. Respond with the id of that
  one existing memory.
- SUPERSEDE: the candidate replaces or updates the current truth of exactly one
  existing memory (for example the value, preference, plan, or state changed).
  Respond with the id of that one existing memory.
- KEEP_BOTH: the candidate and the existing memories can all be true at the same
  time, or the candidate is about a different subject. Respond with target null.

Do NOT decide SUPERSEDE merely because both texts mention the same entity. These
traps are false supersedes:
- different time periods (a 2024 fact and a 2026 fact can coexist);
- multi-valued preferences (liking both tea and coffee is additive, not a change);
- same entity, different attribute (address vs phone vs job are separate facts);
- additive information (a new detail that extends, not replaces);
- historical facts that remain true even when something newer also exists.

Decide SUPERSEDE only when the candidate makes the existing memory's current-truth
claim outdated, such that keeping both would leave contradictory current state.
Choose exactly one existing memory per decision; never invent ids. Return only JSON
matching the response schema."""

D0_CONFLICT_SYSTEM_PROMPT_V2 = """You are the conflict-resolution stage of a long-term
memory system for a Vietnamese chat assistant. A NEW candidate memory fact is about to
be stored. You receive it together with the existing ACTIVE memories that a semantic
search found as possible conflicts. Decide how the candidate relates to them.

Decisions:
- DUPLICATE: the candidate expresses the same current fact as exactly one existing
  memory (paraphrase, restatement, confirmation, clarification, or an added detail
  that does not change the fact). Keeping both rows would create a redundant
  duplicate. Respond with the id of that one existing memory.
- SUPERSEDE: the candidate makes exactly one existing memory's claim about how things
  are NOW no longer true (the value, preference, plan, role, or state changed).
  Respond with the id of that one existing memory.
- KEEP_BOTH: the candidate and the existing memories can all be true as current
  facts at the same time, or the candidate is about a different subject. Respond
  with target null.

Counterfactual test, applied to every candidate target before deciding: "If the
existing memory and the candidate were BOTH kept as current facts, would they
describe an inconsistent present?" If YES, choose SUPERSEDE. If NO, never choose
SUPERSEDE: choose DUPLICATE when both state the same current fact, otherwise
KEEP_BOTH.

SUPERSEDE requires BOTH conditions:
1. Same semantic slot: the candidate and the target describe the same role, value,
   preference, plan, or state of the same subject.
2. Invalidated current truth: after the candidate, the target's claim about how
   things are now is no longer correct.

Role and state evolution can require SUPERSEDE even when the new sentence sounds
additive. Example: the target says "X is only a supporting view; Y remains the main
scope", and the candidate says "X is now tracked in parallel with Y". The target's
exclusivity claim is invalidated, so the correct decision is SUPERSEDE.

Confirmation is not change. Cues that mark something as UNCHANGED - "still", "is
still", "remains", "as before", "continues to be", "currently still", "after the
temporary exception, still", "has not changed", "does not yet include" - make the
candidate a confirmation, restatement, or clarification of the existing fact, never
an update; the same holds for their Vietnamese equivalents. A detail that extends or
clarifies while leaving the target's current fact fully true is DUPLICATE, not
SUPERSEDE. When the target would remain completely true after the candidate, prefer
DUPLICATE over SUPERSEDE.

KEEP_BOTH covers coexisting facts:
- same entity, acronym, or term with a DIFFERENT predicate: a definition of a term
  and a separate usage rule about that term can coexist;
- same topic family but a different attribute, role, or convention: the candidate
  adds an independent rule without changing the target's current truth;
- different subjects entirely.

Lexical similarity alone never justifies SUPERSEDE: shared entities, shared
acronyms, or similar wording without an invalidated current-truth claim means
KEEP_BOTH or DUPLICATE.

These traps are false supersedes (in all of them the existing memory stays true):
- different time periods (a 2024 fact and a 2026 fact can coexist);
- multi-valued preferences (liking both tea and coffee is additive, not a change);
- same entity, different attribute (address vs phone vs job are separate facts);
- additive information (a new detail that extends, not replaces);
- historical facts that remain true even when something newer also exists;
- confirmations of an unchanged fact ("still X") misread as a change to X.

Choose exactly one existing memory for each target-bearing decision: SUPERSEDE and
DUPLICATE each name exactly one target; KEEP_BOTH always has a null target. Never
invent ids. Return only JSON matching the response schema."""

D0_CONFLICT_PROMPTS: dict[str, str] = {
    D0_CONFLICT_PROMPT_VERSION_V1: D0_CONFLICT_SYSTEM_PROMPT,
    D0_CONFLICT_PROMPT_VERSION_V2: D0_CONFLICT_SYSTEM_PROMPT_V2,
}


def resolve_conflict_prompt(version: str) -> tuple[str, str]:
    """(prompt_version, system_prompt) for a registered version; fail-closed on
    unknown versions so a typo can never silently run under the wrong prompt."""
    try:
        return version, D0_CONFLICT_PROMPTS[version]
    except KeyError:
        raise ValueError(
            f"unknown D0 conflict prompt version: {version!r}; "
            f"known: {sorted(D0_CONFLICT_PROMPTS)}"
        ) from None


class D0ProviderError(RuntimeError):
    """Typed infrastructure failure; never converted into a model decision."""

    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def _provider_reason(status: int) -> str:
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limit"
    return "http_status"


def production_preprocess(text: str) -> str:
    """Exact preprocessing production applies before embedding (openai.py:40)."""
    return text.replace("\n", " ")


class D0EmbeddingPort(EmbeddingPort):
    """OpenAI-compatible /embeddings adapter mirroring production embedder semantics."""

    def __init__(self, client: httpx.AsyncClient, config: EvalConfig) -> None:
        if not config.embedding.configured:
            raise ValueError("D0 embedding port requires a configured embedding provider")
        self._client = client
        self._config = config

    async def _post(self, texts: Sequence[str]) -> list[list[float]]:
        provider: ProviderConfig = self._config.embedding
        assert provider.base_url is not None and provider.model is not None
        body: dict[str, object] = {
            "model": provider.model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if self._config.embedding_dimensions is not None:
            body["dimensions"] = self._config.embedding_dimensions
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
                    api_url(str(provider.base_url), "/embeddings"),
                    json=body,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        raise D0ProviderError(
                            _provider_reason(response.status_code),
                            http_status=response.status_code,
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > self._config.max_response_bytes:
                            raise D0ProviderError("response_too_large")
                        chunks.extend(chunk)
        except TimeoutError:
            raise D0ProviderError("timeout") from None
        except httpx.TimeoutException:
            raise D0ProviderError("timeout") from None
        except httpx.RequestError:
            raise D0ProviderError("connection") from None
        parsed = json.loads(bytes(chunks))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
            raise D0ProviderError("invalid_embedding_response")
        data = parsed["data"]
        if len(data) != len(texts):
            raise D0ProviderError("embedding_count_mismatch")
        vectors: list[list[float]] = []
        dimensions: set[int] = set()
        for item in data:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise D0ProviderError("invalid_embedding_response")
            dimensions.add(len(vector))
            vectors.append([float(v) for v in vector])
        if len(dimensions) != 1:
            raise D0ProviderError("embedding_dimension_mismatch")
        expected = self._config.embedding_dimensions
        if expected is not None and dimensions.pop() != expected:
            raise D0ProviderError("embedding_dimension_mismatch")
        return vectors

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [production_preprocess(text) for text in texts]
        vectors: list[list[float]] = []
        # Production embed_batch chunks at 100 inputs per API call (openai.py:61-87).
        for start in range(0, len(prepared), EMBEDDING_BATCH_LIMIT):
            vectors.extend(await self._post(prepared[start : start + EMBEDDING_BATCH_LIMIT]))
        return vectors


class D0DecisionPort(DecisionPort):
    """LLM#2 conflict-decision client over the repo's existing chat pattern.

    Uses the extraction provider (the same OpenAI-compatible endpoint and model
    production selects for formation LLM#1) with temperature 0 and a strict JSON
    schema. Semantic-invalid outputs raise InvalidDecision (executor caches them);
    infrastructure failures raise DecisionPortError (never faked as a decision).
    ``prompt_version`` selects a registered D0 conflict prompt; the version string
    also feeds the executor's request hash, so different prompts never share cache
    entries.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: EvalConfig,
        *,
        prompt_version: str = D0_CONFLICT_PROMPT_VERSION,
    ) -> None:
        if not config.extraction.configured:
            raise ValueError("D0 decision port requires a configured extraction provider")
        self._client = client
        self._config = config
        self._prompt_version, self._system_prompt = resolve_conflict_prompt(prompt_version)

    async def decide(
        self, request_hash: str, request: Mapping[str, object]
    ) -> Mapping[str, object]:
        provider = self._config.extraction
        assert provider.base_url is not None and provider.model is not None
        candidates = request["candidates"]
        assert isinstance(candidates, list)
        candidate_lines = "\n".join(
            f"- id={entry['memory_id']}: {entry['text']}" for entry in candidates
        )
        user_payload = json.dumps(
            {
                "new_candidate_fact": request["candidate_text"],
                "existing_active_memories": candidate_lines,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        body = {
            "model": provider.model,
            "stream": False,
            "temperature": 0.0,
            "max_tokens": self._config.judge_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "d0_conflict_decision",
                    "strict": True,
                    "schema": CONFLICT_RESPONSE_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_payload},
            ],
        }
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
                    api_url(str(provider.base_url), "/chat/completions"),
                    json=body,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        raise D0ProviderError(
                            _provider_reason(response.status_code),
                            http_status=response.status_code,
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > self._config.max_response_bytes:
                            raise D0ProviderError("response_too_large")
                        chunks.extend(chunk)
        except TimeoutError:
            raise D0ProviderError("timeout") from None
        except httpx.TimeoutException:
            raise D0ProviderError("timeout") from None
        except httpx.RequestError:
            raise D0ProviderError("connection") from None
        parsed = json.loads(bytes(chunks))
        try:
            if not isinstance(parsed, dict):
                raise ValueError
            choices = parsed["choices"]
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
            raise D0ProviderError("invalid_chat_response") from None
        try:
            decision_payload = json.loads(content)
        except ValueError:
            raise InvalidDecision("invalid_json") from None
        if not isinstance(decision_payload, dict):
            raise InvalidDecision("invalid_json")
        return decision_payload


def d0_manifest(
    *,
    run_kind: Literal["dry_run", "real_run"],
    timeline: ShadowTimeline,
    bundles: Sequence[object],
    config: EvalConfig,
    embedding_provider: str,
    decision_provider: str,
    prompt_version: str = D0_CONFLICT_PROMPT_VERSION,
    top_k: int = 10,
    config_ids: tuple[str, ...] = ("S0", "S1"),
    git_commit: str,
    git_dirty: bool,
    expected_unique_llm_requests: int,
    expected_llm_calls_without_cache: int,
) -> dict[str, object]:
    """Assemble the run manifest from dataset + config facts only. ``bundles`` is
    the tuple of LoadedBundle objects backing the timeline (dataset identity)."""
    first = bundles[0]
    manifest_doc = first.manifest  # type: ignore[attr-defined]
    dataset_version = str(manifest_doc.dataset_version)
    dataset_status = str(manifest_doc.status)
    review_statuses = {str(entry.review.status) for entry in manifest_doc.bundles}
    stats = timeline.corpus_stats()
    from evaluation.d0_local_executor import response_schema_fingerprint
    from hashlib import sha256 as _sha256

    prompt_hash = _sha256(resolve_conflict_prompt(prompt_version)[1].encode("utf-8")).hexdigest()
    from evaluation.models import D0RunManifest

    manifest = D0RunManifest(
        run_kind=run_kind,
        git_commit=git_commit,
        git_dirty=git_dirty,
        dataset_id="kira_ltm_v1",
        dataset_version=dataset_version,
        dataset_status=dataset_status,
        dataset_review_status="/".join(sorted(review_statuses)),
        embedding_provider=embedding_provider,
        embedding_model=str(config.embedding.model),
        embedding_dimensions=int(config.embedding_dimensions or 0),
        decision_provider=decision_provider,
        decision_model=str(config.extraction.model),
        prompt_version=prompt_version,
        prompt_sha256=prompt_hash,
        response_schema_sha256=response_schema_fingerprint(),
        decoding_params=dict(DEFAULT_DECODING_PARAMS_REF),
        schedules=("early", "late"),
        retrieval_configs=config_ids,
        top_k=top_k,
        corpus_stats=stats,
        expected_oo_points=stats.conflict_evaluable_events * len(config_ids) * 2,
        expected_unique_llm_requests=expected_unique_llm_requests,
        expected_llm_calls_without_cache=expected_llm_calls_without_cache,
        known_limitations=(
            "oracle candidates and oracle shadow bank (O/O), not native extraction",
            "CONVERSATION scope only; no GLOBAL or cross-scope cases in dataset",
            "full-corpus evaluation; no unseen holdout",
            "gold review status is draft, not reviewed",
            "formation boundary ambiguity represented by EARLY/LATE sensitivity bounds",
            "hard-negative slices (GLOBAL, cross-scope, KPI-period, exact-hash) missing",
            "semantic-only offline retrieval; production lexical search is PostgreSQL "
            "FTS ts_rank_cd and is not reproduced",
        ),
    )
    return manifest.model_dump()
