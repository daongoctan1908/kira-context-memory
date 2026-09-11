"""Versioned evaluation contracts, independent of providers and database SDKs."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


NonEmpty = Annotated[str, StringConstraints(min_length=1), AfterValidator(nonblank)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Profile(StrEnum):
    MOCK = "mock"
    EXTERNAL_SYNTHETIC = "external_synthetic"
    INTERNAL_TEST = "internal_test"


class Suite(StrEnum):
    FORMATION = "formation"
    RETRIEVAL = "retrieval"
    REWRITE = "rewrite"
    CROSS_SESSION = "cross_session"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    NOT_RUN = "NOT_RUN"


class Message(EvalModel):
    message_id: Identifier
    role: Literal["user", "assistant"]
    content: NonEmpty


class GoldFact(EvalModel):
    gold_id: Identifier
    text: NonEmpty
    evidence_message_ids: tuple[Identifier, ...] = Field(min_length=1)
    attributed_to: Literal["user", "assistant"]


class SeedMemory(EvalModel):
    gold_id: Identifier
    user_id: Identifier
    text: NonEmpty


class FormationInput(EvalModel):
    kind: Literal["formation"] = "formation"
    user_id: Identifier
    messages: tuple[Message, ...] = Field(min_length=1)


class RetrievalInput(EvalModel):
    kind: Literal["retrieval"] = "retrieval"
    user_id: Identifier
    current_query: NonEmpty
    memories: tuple[SeedMemory, ...] = ()


class RewriteInput(EvalModel):
    kind: Literal["rewrite"] = "rewrite"
    current_query: NonEmpty
    recent_messages: tuple[Message, ...] = ()
    long_term_memories: tuple[SeedMemory, ...] = ()


class CrossSessionInput(EvalModel):
    kind: Literal["cross_session"] = "cross_session"
    user_id: Identifier
    session_a: Identifier
    session_b: Identifier
    session_a_messages: tuple[Message, ...] = Field(min_length=1)
    session_b_query: NonEmpty

    @model_validator(mode="after")
    def distinct_sessions(self) -> "CrossSessionInput":
        if self.session_a == self.session_b:
            raise ValueError("cross-session case requires distinct sessions")
        return self


CaseInput = Annotated[
    FormationInput | RetrievalInput | RewriteInput | CrossSessionInput,
    Field(discriminator="kind"),
]


class GoldSpecification(EvalModel):
    facts: tuple[GoldFact, ...] = ()
    relevant_memory_ids: tuple[Identifier, ...] = ()
    required_exact: tuple[NonEmpty, ...] = ()
    forbidden: tuple[NonEmpty, ...] = ()
    semantic_expectation: NonEmpty


class GoldReview(EvalModel):
    status: Literal["draft", "reviewed"] = "draft"
    reviewer: Identifier | None = None
    revision: Identifier | None = None

    @model_validator(mode="after")
    def reviewed_has_provenance(self) -> "GoldReview":
        if self.status == "reviewed" and (not self.reviewer or not self.revision):
            raise ValueError("reviewed gold requires reviewer and revision")
        return self


class EvalCase(EvalModel):
    schema_version: Literal[1] = 1
    case_id: Identifier
    family_id: Identifier
    split: Literal["dev", "holdout"]
    provenance: Literal["synthetic"]
    tags: tuple[Identifier, ...] = ()
    inputs: CaseInput
    gold: GoldSpecification
    review: GoldReview = Field(default_factory=GoldReview)

    @property
    def suite(self) -> Suite:
        return Suite(self.inputs.kind)


class CaseResult(EvalModel):
    """Semantic evaluation result; a preflight success must not construct this as PASS."""

    schema_version: Literal[1] = 1
    case_id: Identifier
    run_id: UUID
    outcome: Outcome = Outcome.NOT_RUN
    output_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    review_revision: Identifier | None = None
    reason_codes: tuple[Identifier, ...] = ()


class Probe(StrEnum):
    EXTRACTION_JSON = "extraction_json"
    REWRITE_CHAT = "rewrite_chat"
    EMBEDDING_BATCH = "embedding_batch"
    PGVECTOR = "pgvector"
    MEMORY_SCHEMA = "memory_schema"
    CONVERSATION_DB = "conversation_db"
    GATEWAY = "gateway"
    WORKER = "worker"
    KIRA = "kira_mock_identity"


class Reason(StrEnum):
    MISSING_CONFIG = "missing_config"
    PREREQUISITE_FAILED = "prerequisite_failed"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "authentication"
    RATE_LIMIT = "rate_limit"
    HTTP_STATUS = "http_status"
    INVALID_JSON = "invalid_json"
    INVALID_CHAT = "invalid_chat"
    INVALID_EXTRACTION = "invalid_extraction"
    INVALID_EMBEDDING = "invalid_embedding"
    DIMENSION_MISMATCH = "dimension_mismatch"
    RESPONSE_TOO_LARGE = "response_too_large"
    DATABASE = "database"
    SCHEMA_MISMATCH = "schema_mismatch"
    EXTENSION_MISSING = "extension_missing"
    INVALID_HEALTH = "invalid_health"


class TokenUsage(EvalModel):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class ProbeResult(EvalModel):
    probe: Probe
    outcome: Outcome
    simulated: bool = False
    reason: Reason | None = None
    attempted: bool = False
    latency_ms: float = Field(default=0, ge=0, allow_inf_nan=False)
    http_status: int | None = Field(default=None, ge=100, le=599)
    requested_model: str | None = None
    returned_model: str | None = None
    embedding_dimension: int | None = Field(default=None, ge=1)
    embedding_count: int | None = Field(default=None, ge=0)
    extracted_fact_count: int | None = Field(default=None, ge=0)
    usage: TokenUsage | None = None


class SuiteReadiness(EvalModel):
    suite: Suite
    outcome: Outcome
    required_probes: tuple[Probe, ...]


class PreflightReport(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v1"] = "kira-week5-benchmark-v1"
    scope: Literal["dependency_preflight_only"] = "dependency_preflight_only"
    run_id: UUID
    started_at: datetime
    profile: Profile
    simulated: bool
    config_source: Literal["programmatic", "environment", "file_only", "file_then_environment"] = (
        "programmatic"
    )
    config_sha256: str
    configuration: dict[str, object]
    checks: tuple[ProbeResult, ...]
    suites: tuple[SuiteReadiness, ...]
