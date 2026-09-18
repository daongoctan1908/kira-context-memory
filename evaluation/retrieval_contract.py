"""Frozen retrieval score semantics and declared runtime configurations.

The native Mem0 path has two distinct scores:

* pgvector returns a semantic cosine-similarity score (higher is better);
* Mem0 gates that semantic score with ``threshold``, then combines it with
  normalized BM25 and entity signals and returns a normalized hybrid score.

Keeping those meanings explicit prevents reports from calling the returned
hybrid score a cosine score and prevents invalid threshold comparisons across
different embedding spaces.
"""

import json
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from evaluation.models import EvalModel, Identifier, NonEmpty, Sha256


class RetrievalScoreContract(EvalModel):
    """Static contract for the vendored Mem0 + pgvector retrieval path."""

    schema_version: Literal[1] = 1
    backend: Literal["mem0_pgvector_hybrid"] = "mem0_pgvector_hybrid"
    semantic_score: Literal["pgvector_cosine_similarity"] = "pgvector_cosine_similarity"
    threshold_applies_to: Literal["semantic_score_before_hybrid"] = "semantic_score_before_hybrid"
    returned_score: Literal["normalized_hybrid_score"] = "normalized_hybrid_score"
    score_direction: Literal["higher_is_better"] = "higher_is_better"
    truncation: Literal["rank_descending_then_top_k"] = "rank_descending_then_top_k"
    candidate_overfetch: Literal["max_top_k_times_4_or_60"] = "max_top_k_times_4_or_60"
    user_filter: Literal["exact_user_id"] = "exact_user_id"
    retrieval_depth: Literal[10] = 10
    recall_cutoff: Literal[3] = 3


class RetrievalRuntimeConfig(EvalModel):
    """The production retrieval knobs declared by one benchmark candidate.

    ``retrieval_depth`` remains fixed at 10 in the evaluation contract so MRR
    is observable through rank 10. ``top_k`` here is the candidate's runtime
    context limit and is recorded independently rather than silently changing
    the scorer contract.
    """

    top_k: int = Field(default=10, ge=1, le=10, strict=True)
    threshold: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    embedding_model: NonEmpty
    embedding_dimensions: int = Field(ge=1, le=65536, strict=True)


def retrieval_config_sha256(config: RetrievalRuntimeConfig) -> str:
    """Return a stable, secret-free hash of the complete retrieval config."""

    canonical = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class RetrievalConfigDeclaration(EvalModel):
    config_id: Identifier
    role: Literal["control", "candidate"]
    config: RetrievalRuntimeConfig
    config_sha256: Sha256
    declared_differences: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def hash_and_role_are_consistent(self) -> "RetrievalConfigDeclaration":
        if self.config_sha256 != retrieval_config_sha256(self.config):
            raise ValueError("retrieval config hash does not match declaration")
        if self.role == "control" and self.declared_differences:
            raise ValueError("retrieval control cannot declare differences")
        if self.role == "candidate" and not self.declared_differences:
            raise ValueError("retrieval candidate must declare its differences")
        return self


def declare_retrieval_config(
    *,
    config_id: str,
    role: Literal["control", "candidate"],
    config: RetrievalRuntimeConfig,
    declared_differences: tuple[str, ...] = (),
) -> RetrievalConfigDeclaration:
    return RetrievalConfigDeclaration(
        config_id=config_id,
        role=role,
        config=config,
        config_sha256=retrieval_config_sha256(config),
        declared_differences=declared_differences,
    )


class RetrievalConfigRegistry(EvalModel):
    """At most two comparable retrieval configs: one control and one candidate."""

    schema_version: Literal[1] = 1
    score_contract: RetrievalScoreContract = Field(default_factory=RetrievalScoreContract)
    configs: tuple[RetrievalConfigDeclaration, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def configs_are_comparable(self) -> "RetrievalConfigRegistry":
        ids = [item.config_id for item in self.configs]
        if len(ids) != len(set(ids)):
            raise ValueError("retrieval config IDs must be unique")
        hashes = [item.config_sha256 for item in self.configs]
        if len(hashes) != len(set(hashes)):
            raise ValueError("retrieval configs must be distinct")
        if sum(item.role == "control" for item in self.configs) != 1:
            raise ValueError("retrieval registry requires exactly one control")

        embedding_spaces = {
            (item.config.embedding_model, item.config.embedding_dimensions) for item in self.configs
        }
        if len(embedding_spaces) != 1:
            raise ValueError(
                "retrieval thresholds cannot be compared across embedding models or dimensions"
            )
        return self
