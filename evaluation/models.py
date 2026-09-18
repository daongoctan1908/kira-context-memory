"""Versioned evaluation contracts, independent of providers and database SDKs."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

BENCHMARK_CONTRACT_ID = "kira-week5-benchmark-v3"
HISTORICAL_CONTROL_SHA = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


NonEmpty = Annotated[str, StringConstraints(min_length=1), AfterValidator(nonblank)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40,64}$")]


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
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    NOT_RUN = "NOT_RUN"


class PerformanceReviewVerdict(StrEnum):
    ACCEPTABLE = "acceptable"
    REJECT_REGRESSION = "reject_regression"
    NEEDS_MORE_SAMPLES = "needs_more_samples"


class BenchmarkVariant(StrEnum):
    WORKING_TREE = "working_tree"
    HISTORICAL_CONTROL = "historical_control"
    RELEASE_CANDIDATE = "release_candidate"


class CandidateChangeScope(StrEnum):
    PROMPT = "prompt"
    CONFIG = "config"
    RUNTIME_CODE = "runtime_code"
    DEPENDENCIES = "dependencies"
    SCHEMA = "schema"
    LIFECYCLE = "lifecycle"


class GitSource(EvalModel):
    sha: GitSha
    dirty: bool


class CandidateDeclaration(EvalModel):
    candidate_id: Identifier
    control_runtime_sha: GitSha = HISTORICAL_CONTROL_SHA
    change_scopes: tuple[CandidateChangeScope, ...] = Field(min_length=1)
    summary: NonEmpty

    @field_validator("change_scopes")
    @classmethod
    def change_scopes_are_unique(
        cls, value: tuple[CandidateChangeScope, ...]
    ) -> tuple[CandidateChangeScope, ...]:
        if len(value) != len(set(value)):
            raise ValueError("candidate change scopes must be unique")
        return value


class RunProvenance(EvalModel):
    """Identity of executed runtime and harness; never infer one from the other."""

    variant: BenchmarkVariant
    runtime: GitSource
    harness: GitSource
    prompt_sha256: dict[Identifier, Sha256] = Field(min_length=1)
    package_versions: dict[Identifier, NonEmpty] = Field(min_length=1)
    candidate: CandidateDeclaration | None = None

    @model_validator(mode="after")
    def variant_has_consistent_declaration(self) -> "RunProvenance":
        required_prompts = {"memory_extraction", "rewrite_system"}
        required_packages = {"kira-context-memory", "viettel-mem0"}
        if missing := required_prompts.difference(self.prompt_sha256):
            raise ValueError(f"run provenance is missing prompt hashes: {sorted(missing)}")
        if missing := required_packages.difference(self.package_versions):
            raise ValueError(f"run provenance is missing package versions: {sorted(missing)}")
        if self.variant is BenchmarkVariant.HISTORICAL_CONTROL:
            if self.runtime.sha != HISTORICAL_CONTROL_SHA:
                raise ValueError("historical control must use the frozen control runtime SHA")
            if self.candidate is not None:
                raise ValueError("historical control cannot contain a candidate declaration")
            if self.package_versions["kira-context-memory"] != "0.4.1":
                raise ValueError("historical control must retain application version 0.4.1")
            if self.package_versions["viettel-mem0"] != "2.0.20+viettel.3":
                raise ValueError("historical control must retain viettel-mem0 version .3")
        elif self.variant is BenchmarkVariant.RELEASE_CANDIDATE:
            if self.candidate is None:
                raise ValueError("release candidate requires an explicit change declaration")
        elif self.candidate is not None:
            raise ValueError("working-tree evidence cannot claim a release candidate declaration")
        return self

    @computed_field
    @property
    def attribution_scope(
        self,
    ) -> Literal[
        "working_tree",
        "historical_control",
        "prompt_or_config_candidate",
        "mixed_runtime_candidate",
    ]:
        if self.variant is BenchmarkVariant.WORKING_TREE:
            return "working_tree"
        if self.variant is BenchmarkVariant.HISTORICAL_CONTROL:
            return "historical_control"
        assert self.candidate is not None
        non_prompt_scopes = {
            CandidateChangeScope.RUNTIME_CODE,
            CandidateChangeScope.DEPENDENCIES,
            CandidateChangeScope.SCHEMA,
            CandidateChangeScope.LIFECYCLE,
        }
        if non_prompt_scopes.intersection(self.candidate.change_scopes):
            return "mixed_runtime_candidate"
        return "prompt_or_config_candidate"


class Message(EvalModel):
    message_id: Identifier
    session_id: Identifier | None = None
    role: Literal["user", "assistant"]
    content: NonEmpty
    timestamp: datetime | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("message timestamp must be timezone-aware")
        return value


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
    source_session_ids: tuple[Identifier, ...] = ()

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
    supporting_memory_ids: tuple[Identifier, ...] = ()
    history_memory_ids: tuple[Identifier, ...] = ()
    required_exact: tuple[NonEmpty, ...] = ()
    forbidden: tuple[NonEmpty, ...] = ()
    semantic_expectation: NonEmpty
    expected_answer: NonEmpty | None = None
    expected_rewrite: NonEmpty | None = None
    expected_action: Identifier | None = None
    expected_api: dict[str, Any] | None = None
    no_hit_fpr_eligible: bool | None = None
    lifecycle_event: "FormationLifecycleGold | None" = None


class FormationLifecycleGold(EvalModel):
    event_id: Identifier
    should_store: bool
    expected_operation: Literal["add", "update", "reinforce_existing", "do_not_persist"]
    active_at_end: bool
    memory_family: Identifier
    related_event_ids: tuple[Identifier, ...] = ()


class CaseEligibility(EvalModel):
    status: Literal["eligible", "blocked"] = "eligible"
    blocked_reasons: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def status_matches_reasons(self) -> "CaseEligibility":
        if self.status == "eligible" and self.blocked_reasons:
            raise ValueError("eligible case must not have blocked reasons")
        if self.status == "blocked" and not self.blocked_reasons:
            raise ValueError("blocked case requires at least one reason")
        if len(set(self.blocked_reasons)) != len(self.blocked_reasons):
            raise ValueError("blocked reasons must be unique")
        return self


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
    evaluation_scope: Literal["full_corpus"]
    provenance: Literal["synthetic"]
    tags: tuple[Identifier, ...] = ()
    source_row_ids: tuple[Identifier, ...] = Field(min_length=1)
    eligibility: CaseEligibility = Field(default_factory=CaseEligibility)
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
    contract_id: Literal["kira-week5-benchmark-v3"] = BENCHMARK_CONTRACT_ID
    scope: Literal["dependency_preflight_only"] = "dependency_preflight_only"
    run_id: UUID
    started_at: datetime
    profile: Profile
    simulated: bool
    config_source: Literal["programmatic", "environment", "file_only", "file_then_environment"] = (
        "programmatic"
    )
    config_sha256: Sha256
    provenance: RunProvenance
    configuration: dict[str, object]
    checks: tuple[ProbeResult, ...]
    suites: tuple[SuiteReadiness, ...]
