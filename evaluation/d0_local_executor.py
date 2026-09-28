"""D0-local O/O conflict-stage executor (exploratory characterization).

Runs oracle candidates (gold canonical facts) against an oracle shadow ACTIVE bank
under EARLY and LATE formation schedules with semantic-only retrieval. Execution is
independent of gold scoring: whenever the retrieved pool is non-empty LLM#2 always
runs, even when the gold target was not retrieved. Production lexical search is
PostgreSQL FTS ``ts_rank_cd``; these offline configs are semantic-only and must
never be reported as hybrid or production-like.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from itertools import groupby

from evaluation.models import (
    D0ConflictDecision,
    D0Decision,
    D0Prediction,
    D0RetrievalConfig,
    D0RetrievalResult,
    D0Schedule,
    Identifier,
)
from evaluation.shadow_lifecycle import GoldEvent, ScheduleBundle, ShadowTimeline

OVERFETCH_MULTIPLIER = 4
OVERFETCH_MINIMUM = 60


def s0_config() -> D0RetrievalConfig:
    return D0RetrievalConfig(config_id="S0", semantic_floor=0.0)


def s1_config() -> D0RetrievalConfig:
    return D0RetrievalConfig(config_id="S1", semantic_floor=0.1)


def s_sweep_config(threshold: float) -> D0RetrievalConfig:
    return D0RetrievalConfig(config_id="S_SWEEP", semantic_floor=threshold, label_informed=True)


class EmbeddingPortError(RuntimeError):
    pass


class EmbeddingPort:
    """Read-only embedding access; production behavior is never mutated."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    @staticmethod
    def cosine(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        norm_left = sum(a * a for a in left) ** 0.5
        norm_right = sum(b * b for b in right) ** 0.5
        if norm_left == 0.0 or norm_right == 0.0:
            return 0.0
        return numerator / (norm_left * norm_right)


class DecisionPortError(RuntimeError):
    pass


class DecisionPort:
    """LLM#2 conflict decision client; strict contract validated by the caller."""

    async def decide(self, request_hash: str, request: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError


def _overfetch_limit(top_k: int) -> int:
    return max(top_k * OVERFETCH_MULTIPLIER, OVERFETCH_MINIMUM)


def retrieve_semantic(
    query_embedding: list[float],
    bank: Mapping[str, tuple[str, list[float]]],
    config: D0RetrievalConfig,
    top_k: int,
) -> D0RetrievalResult:
    """Semantic-only retrieval mirroring production ordering: over-fetch ->
    semantic floor gate on raw cosine score -> rank -> cut top-k."""
    if not bank:
        return D0RetrievalResult(pool_size=0)
    scored: list[tuple[str, float]] = []
    for memory_id, (text, embedding) in bank.items():
        score = max(0.0, EmbeddingPort.cosine(query_embedding, embedding))
        if score < config.semantic_floor:
            continue
        scored.append((memory_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    retrieved = tuple(memory_id for memory_id, _ in scored[:top_k])
    return D0RetrievalResult(
        retrieved_ids=retrieved,
        scored_ids=tuple(memory_id for memory_id, _ in scored),
        pool_size=len(scored),
    )


class InvalidDecision(ValueError):
    """LLM#2 output violated the strict contract (fail-closed)."""


CONFLICT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "target_memory_id"],
    "properties": {
        "decision": {"type": "string", "enum": ["DUPLICATE", "KEEP_BOTH", "SUPERSEDE"]},
        "target_memory_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
}

DEFAULT_DECODING_PARAMS: dict[str, object] = {"temperature": 0.0, "max_tokens": 512}


def response_schema_fingerprint() -> str:
    canonical = json.dumps(
        CONFLICT_RESPONSE_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _parse_decision(raw: Mapping[str, object], pool_ids: tuple[str, ...]) -> D0ConflictDecision:
    decision_raw = raw.get("decision")
    target = raw.get("target_memory_id")
    if not isinstance(decision_raw, str) or decision_raw not in (
        "DUPLICATE",
        "KEEP_BOTH",
        "SUPERSEDE",
    ):
        raise InvalidDecision("invalid_decision")
    if target is not None and (not isinstance(target, str) or not target):
        raise InvalidDecision("invalid_target")
    if decision_raw == "KEEP_BOTH":
        if target is not None:
            raise InvalidDecision("invalid_target")
        return D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None)
    if not isinstance(target, str):
        raise InvalidDecision("invalid_target")
    if target not in pool_ids:
        raise InvalidDecision("invalid_target")
    return D0ConflictDecision(decision=decision_raw, target_memory_id=target)  # type: ignore[arg-type]


def decision_request(
    model_id: str,
    prompt_version: str,
    candidate_text: str,
    pool: D0RetrievalResult,
    bank_texts: Mapping[str, str],
    *,
    decoding_params: Mapping[str, object] = DEFAULT_DECODING_PARAMS,
    schema_fingerprint: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Deterministic request identity from every output-affecting parameter: provider
    model id, explicit decoding params, prompt version, response-schema fingerprint,
    candidate text, ordered candidate IDs and texts, and the operation set. No
    secrets are hashed; retrieval provenance (schedule, config, floor) is absent
    because it does not appear in the actual LLM request — equivalent requests
    share one cache entry."""
    ordered_ids = pool.retrieved_ids
    request = {
        "model": model_id,
        "decoding_params": dict(decoding_params),
        "prompt_version": prompt_version,
        "response_schema_sha256": schema_fingerprint or response_schema_fingerprint(),
        "candidate_text": candidate_text,
        "candidates": [
            {"memory_id": memory_id, "text": bank_texts[memory_id]} for memory_id in ordered_ids
        ],
        "operations": ["DUPLICATE", "KEEP_BOTH", "SUPERSEDE"],
    }
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest(), request


class D0LocalExecutor:
    """Evaluate one bundle timeline under both schedules with paired caching."""

    def __init__(
        self,
        timeline: ShadowTimeline,
        embeddings: EmbeddingPort,
        decisions: DecisionPort,
        *,
        model_id: str = "d0-local",
        prompt_version: str = "d0-conflict-v1",
        top_k: int = 10,
    ) -> None:
        self._timeline = timeline
        self._embeddings = embeddings
        self._decisions = decisions
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._top_k = top_k
        self._decision_cache: dict[str, D0ConflictDecision | InvalidDecision] = {}

    async def _llm2(
        self,
        candidate_text: str,
        pool: D0RetrievalResult,
        bank: Mapping[str, GoldEvent],
    ) -> tuple[D0ConflictDecision | None, str | None, bool]:
        """Run LLM#2 on the non-empty pool; identical requests reuse cached decisions.

        Semantic-invalid outputs (contract violations) are deterministic model
        behavior and are cached with cache_hit=True on repeat. Transport failures
        (DecisionPortError) are infrastructure: never cached, never faked as a
        decision — retry policy belongs to the port implementation."""
        bank_texts = {event_id: event.canonical_fact for event_id, event in bank.items()}
        request_hash, request = decision_request(
            self._model_id, self._prompt_version, candidate_text, pool, bank_texts
        )
        cached = self._decision_cache.get(request_hash)
        if cached is not None:
            if isinstance(cached, InvalidDecision):
                return None, request_hash, True
            return cached, request_hash, True
        try:
            raw = await self._decisions.decide(request_hash, request)
            decision = _parse_decision(raw, pool.retrieved_ids)
        except InvalidDecision as error:
            self._decision_cache[request_hash] = error
            return None, request_hash, False
        self._decision_cache[request_hash] = decision
        return decision, request_hash, False

    async def evaluate_bundle_schedule(
        self,
        bundle_id: str,
        schedule: D0Schedule,
        configs: Sequence[D0RetrievalConfig],
    ) -> list[D0Prediction]:
        bundle = self._timeline.bundle(schedule, bundle_id)
        ambiguous = self._timeline.ambiguous_aggregate_events()
        clipped = {
            f"{bundle_id}:{event.event_id}"
            for event in bundle.events
            if _evidence_clipped(bundle, event)
        }
        predictions: list[D0Prediction] = []
        for _, batch, active_ids in bundle.iter_batches():
            # Same-boundary candidates share one pre-batch snapshot; gold transitions
            # are applied only after the whole batch has been evaluated.
            texts = [event.canonical_fact for event in batch]
            candidate_embeddings = await self._embeddings.embed(texts)
            bank_events = {event_id: _bank_event(bundle, event_id) for event_id in active_ids}
            bank_embeddings = (
                await self._embeddings.embed([e.canonical_fact for e in bank_events.values()])
                if bank_events
                else []
            )
            bank_vectors = {
                event_id: (event.canonical_fact, embedding)
                for (event_id, event), embedding in zip(
                    bank_events.items(), bank_embeddings, strict=True
                )
            }
            for event, candidate_embedding in zip(batch, candidate_embeddings, strict=True):
                for config in configs:
                    pool = retrieve_semantic(candidate_embedding, bank_vectors, config, self._top_k)
                    if pool.retrieved_ids:
                        decision, request_hash, cache_hit = await self._llm2(
                            event.canonical_fact, pool, bank_events
                        )
                    else:
                        decision = D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None)
                        request_hash, cache_hit = None, False
                    predictions.append(
                        D0Prediction(
                            event_id=f"{bundle_id}:{event.event_id}",
                            bundle_id=bundle_id,
                            schedule=schedule,
                            config_id=config.config_id,
                            gold_operation=event.expected_operation,  # type: ignore[arg-type]
                            gold_target_ids=event.gold_target_ids,
                            candidate_text=event.canonical_fact,
                            pool=pool,
                            decision=decision,
                            llm_request_hash=request_hash,
                            cache_hit=cache_hit,
                            retrieval_failure=bool(event.gold_target_ids)
                            and not any(
                                target in pool.retrieved_ids for target in event.gold_target_ids
                            ),
                            evidence_clipped=f"{bundle_id}:{event.event_id}" in clipped,
                            ambiguous_aggregate=f"{bundle_id}:{event.event_id}" in ambiguous,
                        )
                    )
        return predictions

    async def evaluate_all(
        self,
        configs: Sequence[D0RetrievalConfig] | None = None,
    ) -> list[D0Prediction]:
        configs = tuple(configs) if configs is not None else (s0_config(), s1_config())
        predictions: list[D0Prediction] = []
        for bundle_id in self._timeline.bundle_ids():
            for schedule in D0Schedule:
                predictions.extend(await self.evaluate_bundle_schedule(bundle_id, schedule, configs))
        return predictions


def _bank_event(bundle: ScheduleBundle, event_id: str) -> GoldEvent:
    for event in bundle.events:
        if event.event_id == event_id:
            return event
    raise KeyError(event_id)


def _evidence_clipped(bundle: ScheduleBundle, event: GoldEvent) -> bool:
    """Evidence turns beyond the EARLY boundary of the same event."""
    if bundle.schedule is not D0Schedule.EARLY:
        return False
    boundary_position = bundle.turn_index.order[bundle.boundaries[event.event_id]]
    return any(
        bundle.turn_index.order[turn] > boundary_position for turn in event.evidence_turns
    )
