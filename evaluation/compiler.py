"""Compile the canonical KiRa dataset into deterministic benchmark cases."""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from evaluation.dataset import (
    DatasetManifest,
    DatasetMessage,
    LoadedBundle,
    default_dataset_root,
    iter_messages,
    load_bundle,
    load_manifest,
    namespace_id,
)
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    CaseEligibility,
    CrossSessionInput,
    EvalCase,
    EvalModel,
    FormationInput,
    FormationLifecycleGold,
    GoldFact,
    GoldSpecification,
    Identifier,
    Message,
    RetrievalInput,
    RewriteInput,
    SeedMemory,
    Sha256,
    Suite,
)

SourceKind = Literal["conversation", "fill", "memory", "qa"]
CoverageState = Literal["linked", "accounted_not_selected"]
_PENDING_ANSWER = "TBD_AFTER_KIRA_FILL"


class SourceCoverageRow(EvalModel):
    row_id: Identifier
    kind: SourceKind
    roles: tuple[Identifier, ...] = Field(min_length=1)
    case_ids: tuple[Identifier, ...] = ()
    state: CoverageState
    reason: Identifier | None = None

    @model_validator(mode="after")
    def state_is_consistent(self) -> "SourceCoverageRow":
        if self.state == "linked" and not self.case_ids:
            raise ValueError("linked source row requires a case")
        if self.state == "accounted_not_selected" and self.case_ids:
            raise ValueError("unselected source row must not reference a case")
        if self.state == "accounted_not_selected" and self.reason is None:
            raise ValueError("unselected source row requires a reason")
        return self


class SuiteCoverage(EvalModel):
    suite: Suite
    total: int = Field(ge=0, strict=True)
    eligible: int = Field(ge=0, strict=True)
    blocked: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_add_up(self) -> "SuiteCoverage":
        if self.total != self.eligible + self.blocked:
            raise ValueError("suite coverage counts do not add up")
        return self


class SourceCoverage(EvalModel):
    kind: SourceKind
    total: int = Field(ge=0, strict=True)
    linked: int = Field(ge=0, strict=True)
    accounted_not_selected: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_add_up(self) -> "SourceCoverage":
        if self.total != self.linked + self.accounted_not_selected:
            raise ValueError("source coverage counts do not add up")
        return self


class BlockedReasonCoverage(EvalModel):
    reason: Identifier
    cases: int = Field(ge=1, strict=True)


class CompilationCoverage(EvalModel):
    suites: tuple[SuiteCoverage, ...]
    sources: tuple[SourceCoverage, ...]
    blocked_reasons: tuple[BlockedReasonCoverage, ...]
    rows: tuple[SourceCoverageRow, ...]


class DatasetCompilation(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    dataset_id: Identifier
    dataset_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    dataset_sha256: Sha256
    seed: int = Field(ge=0, le=2**63 - 1, strict=True)
    cases: tuple[EvalCase, ...]
    coverage: CompilationCoverage

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> "DatasetCompilation":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("compiled case IDs must be unique")
        row_ids = [row.row_id for row in self.coverage.rows]
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("source coverage row IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class _BundleContext:
    bundle: LoadedBundle
    messages: tuple[DatasetMessage, ...]
    by_local_id: Mapping[str, DatasetMessage]
    companions: Mapping[str, str]
    fill_by_message_id: Mapping[str, str]
    user_id: str


@dataclass(slots=True)
class _MutableCoverage:
    kind: SourceKind
    roles: set[str]
    case_ids: set[str]


def compilation_json_bytes(compilation: DatasetCompilation) -> bytes:
    """Return a stable, path-independent representation suitable for hashing/artifacts."""

    payload = compilation.model_dump(mode="json", by_alias=True, exclude_none=False)
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _source_row_id(bundle_id: str, kind: SourceKind, local_id: str) -> str:
    return namespace_id(bundle_id, f"{kind}:{local_id}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dataset_fingerprint(root: Path, manifest: DatasetManifest) -> str:
    parts: list[tuple[str, str]] = []
    manifest_path = root / "manifest.json"
    parts.append(("manifest.json", _sha256(manifest_path.read_bytes())))
    for entry in manifest.bundles:
        for _, descriptor in entry.files.items():
            relative = f"{entry.path}/{descriptor.name}"
            actual = _sha256((root / entry.path / descriptor.name).read_bytes())
            if actual != descriptor.sha256:
                raise ValueError(f"dataset checksum mismatch: {relative}")
            parts.append((relative, actual))
    canonical = json.dumps(sorted(parts), ensure_ascii=True, separators=(",", ":")).encode()
    return _sha256(canonical)


def _document_rows(document: Mapping[str, Any], key: str, *, label: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


def _bundle_context(bundle: LoadedBundle) -> _BundleContext:
    messages = tuple(iter_messages(bundle))
    by_local_id: dict[str, DatasetMessage] = {}
    per_session: dict[str, list[DatasetMessage]] = defaultdict(list)
    for message in messages:
        if message.local_message_id in by_local_id:
            raise ValueError(
                f"duplicate conversation message ID: {bundle.manifest.bundle_id}:"
                f"{message.local_message_id}"
            )
        by_local_id[message.local_message_id] = message
        per_session[message.session_id].append(message)

    companions: dict[str, str] = {}
    for session_messages in per_session.values():
        for index, message in enumerate(session_messages):
            if (
                message.role == "user"
                and index + 1 < len(session_messages)
                and session_messages[index + 1].role == "assistant"
            ):
                companions[message.local_message_id] = session_messages[index + 1].local_message_id
            elif (
                message.role == "assistant"
                and index > 0
                and session_messages[index - 1].role == "user"
            ):
                companions[message.local_message_id] = session_messages[index - 1].local_message_id

    fill_by_message_id: dict[str, str] = {}
    for fill in _document_rows(
        bundle.fills,
        "fills",
        label=f"{bundle.manifest.bundle_id}/fills",
    ):
        fill_id = str(fill.get("fill_id", ""))
        if not fill_id:
            raise ValueError("fill row is missing fill_id")
        for field in ("user_turn_id", "assistant_turn_id"):
            message_id = str(fill.get(field, ""))
            if not message_id:
                raise ValueError(f"{fill_id} is missing {field}")
            existing = fill_by_message_id.setdefault(message_id, fill_id)
            if existing != fill_id:
                raise ValueError(f"message {message_id} belongs to multiple fill rows")

    return _BundleContext(
        bundle=bundle,
        messages=messages,
        by_local_id=by_local_id,
        companions=companions,
        fill_by_message_id=fill_by_message_id,
        user_id=namespace_id(bundle.manifest.bundle_id, "user"),
    )


def _evidence_window(
    context: _BundleContext,
    local_ids: Iterable[str],
) -> tuple[tuple[Message, ...], tuple[str, ...], tuple[str, ...]]:
    selected: set[str] = set()
    for local_id in local_ids:
        if local_id not in context.by_local_id:
            raise ValueError(
                f"unknown evidence message: {context.bundle.manifest.bundle_id}:{local_id}"
            )
        selected.add(local_id)
        if companion := context.companions.get(local_id):
            selected.add(companion)

    pending: list[str] = []
    result: list[Message] = []
    source_rows: list[str] = []
    fill_rows: set[str] = set()
    for message in context.messages:
        if message.local_message_id not in selected:
            continue
        source_rows.append(
            _source_row_id(
                context.bundle.manifest.bundle_id,
                "conversation",
                message.local_message_id,
            )
        )
        if fill_id := context.fill_by_message_id.get(message.local_message_id):
            fill_rows.add(_source_row_id(context.bundle.manifest.bundle_id, "fill", fill_id))
        if not message.content.strip():
            pending.append(message.message_id)
            continue
        result.append(
            Message(
                message_id=message.message_id,
                session_id=message.session_id,
                role=message.role,
                content=message.content,
                timestamp=message.timestamp,
            )
        )
    if not result:
        raise ValueError("evidence window has no materialized messages")
    return tuple(result), tuple(pending), tuple(source_rows + sorted(fill_rows))


def _identifiers(bundle_id: str, values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("expected an array of identifiers")
    return tuple(namespace_id(bundle_id, value) for value in values)


def _tags(bundle_id: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    values = {
        f"bundle:{bundle_id}",
        f"category:{row['category']}",
        f"tier:{row['evaluation_tier']}",
        f"scenario:{row['scenario_group']}",
    }
    values.update(f"family:{value}" for value in row.get("memory_families", []))
    return tuple(sorted(values))


def _eligibility(*reasons: str) -> CaseEligibility:
    unique = tuple(sorted(set(filter(None, reasons))))
    if unique:
        return CaseEligibility(status="blocked", blocked_reasons=unique)
    return CaseEligibility()


def _related_memory_ids(bundle_id: str, memory: Mapping[str, Any]) -> tuple[str, ...]:
    related: set[str] = set()
    for field in ("supersedes_memory_id", "reinforces_memory_id"):
        value = memory.get(field)
        if isinstance(value, str) and value:
            related.add(namespace_id(bundle_id, value))
    values = memory.get("reinforces_memory_ids")
    if isinstance(values, list):
        related.update(namespace_id(bundle_id, str(value)) for value in values)
    return tuple(sorted(related))


def _formation_case(context: _BundleContext, memory: Mapping[str, Any]) -> EvalCase:
    bundle_id = context.bundle.manifest.bundle_id
    local_id = str(memory["memory_id"])
    source_ids = [str(value) for value in memory["source_turn_ids"]]
    # A formation event ends at its actual source pair. Supporting evidence may be a later
    # reinforcement; giving that to extraction would leak future context and change NewMessages.
    source_messages, _, _ = _evidence_window(context, source_ids)
    source_sessions = {message.session_id for message in source_messages}
    if len(source_sessions) != 1:
        raise ValueError("formation source event must belong to one original conversation")
    source_session = next(iter(source_sessions))
    selected_ids = {message.message_id for message in source_messages}
    boundary = max(
        index
        for index, message in enumerate(context.messages)
        if message.message_id in selected_ids
    )
    prefix_ids = [
        message.local_message_id
        for message in context.messages[: boundary + 1]
        if message.session_id == source_session
    ]
    messages, pending, source_rows = _evidence_window(context, prefix_ids)
    boundary_source_ids = (
        tuple(message.message_id for message in messages[-2:]) if not pending else ()
    )
    event_id = namespace_id(bundle_id, local_id)
    should_store = bool(memory["should_store"])
    facts = (
        (
            GoldFact(
                gold_id=event_id,
                text=str(memory["canonical_fact"]),
                evidence_message_ids=tuple(namespace_id(bundle_id, value) for value in source_ids),
                attributed_to="user",
            ),
        )
        if should_store
        else ()
    )
    memory_row = _source_row_id(bundle_id, "memory", local_id)
    return EvalCase(
        case_id=namespace_id(bundle_id, f"formation:{local_id}"),
        family_id=namespace_id(bundle_id, f"memory-family:{memory['memory_family']}"),
        evaluation_scope="full_corpus",
        provenance="synthetic",
        tags=tuple(
            sorted(
                {
                    f"bundle:{bundle_id}",
                    f"family:{memory['memory_family']}",
                    f"operation:{memory['expected_operation']}",
                }
            )
        ),
        source_row_ids=tuple(dict.fromkeys((*source_rows, memory_row))),
        eligibility=_eligibility("pending_kira_assistant" if pending else ""),
        inputs=FormationInput(
            user_id=context.user_id,
            messages=messages,
            source_message_ids=boundary_source_ids,
        ),
        gold=GoldSpecification(
            facts=facts,
            formation_contract="open_world",
            forbidden_facts=(() if should_store else (str(memory["canonical_fact"]),)),
            semantic_expectation=(
                str(memory["canonical_fact"])
                if should_store
                else f"Do not persist the unsupported candidate {event_id}"
            ),
            lifecycle_event=FormationLifecycleGold(
                event_id=event_id,
                should_store=should_store,
                expected_operation=str(memory["expected_operation"]),
                active_at_end=bool(memory["active_at_end"]),
                memory_family=str(memory["memory_family"]),
                related_event_ids=_related_memory_ids(bundle_id, memory),
                memory_scope=(
                    str(memory["memory_scope"])
                    if should_store and "memory_scope" in memory
                    else None
                ),
            ),
        ),
        review=context.bundle.manifest.review,
    )


def _answer(row: Mapping[str, Any]) -> str | None:
    for field in ("final_answer", "gold_answer"):
        value = row.get(field)
        if isinstance(value, str) and value.strip() and value != _PENDING_ANSWER:
            return value
    return None


def _semantic_expectation(row: Mapping[str, Any]) -> str:
    if answer := _answer(row):
        return answer
    rewrite = row.get("gold_rewrite")
    if isinstance(rewrite, str) and rewrite.strip():
        return rewrite
    return f"Expected action: {row['expected_action']}"


def _qa_gold(bundle_id: str, row: Mapping[str, Any]) -> GoldSpecification:
    required = _identifiers(bundle_id, row.get("required_memory_ids", []))
    supporting = _identifiers(bundle_id, row.get("supporting_memory_ids", []))
    history = _identifiers(bundle_id, row.get("history_memory_ids", []))
    explicit_no_hit = row.get("no_hit_fpr_eligible")
    no_hit = (
        bool(explicit_no_hit)
        if isinstance(explicit_no_hit, bool)
        else not (required or supporting or history)
    )
    rewrite = row.get("gold_rewrite")
    expected_api = row.get("expected_api")
    return GoldSpecification(
        relevant_memory_ids=required,
        supporting_memory_ids=supporting,
        history_memory_ids=history,
        semantic_expectation=_semantic_expectation(row),
        expected_answer=_answer(row),
        expected_rewrite=(rewrite if isinstance(rewrite, str) and rewrite.strip() else None),
        expected_action=str(row["expected_action"]),
        expected_api=(expected_api if isinstance(expected_api, dict) else None),
        no_hit_fpr_eligible=no_hit,
    )


def _qa_source_rows(
    bundle_id: str,
    row: Mapping[str, Any],
    seed_memory_ids: Sequence[str],
) -> tuple[str, ...]:
    rows = [_source_row_id(bundle_id, "qa", str(row["question_id"]))]
    rows.extend(_source_row_id(bundle_id, "memory", local_id) for local_id in seed_memory_ids)
    return tuple(dict.fromkeys(rows))


def _retrieval_case(
    context: _BundleContext,
    row: Mapping[str, Any],
    seeds: tuple[SeedMemory, ...],
    seed_local_ids: tuple[str, ...],
) -> EvalCase:
    bundle_id = context.bundle.manifest.bundle_id
    local_id = str(row["question_id"])
    return EvalCase(
        case_id=namespace_id(bundle_id, f"retrieval:{local_id}"),
        family_id=namespace_id(bundle_id, f"scenario:{row['scenario_group']}"),
        evaluation_scope="full_corpus",
        provenance="synthetic",
        tags=_tags(bundle_id, row),
        source_row_ids=_qa_source_rows(bundle_id, row, seed_local_ids),
        inputs=RetrievalInput(
            user_id=context.user_id,
            current_query=str(row["question"]),
            memories=seeds,
        ),
        gold=_qa_gold(bundle_id, row),
        review=context.bundle.manifest.review,
    )


def _rewrite_case(
    context: _BundleContext,
    row: Mapping[str, Any],
    seeds: tuple[SeedMemory, ...],
    seed_local_ids: tuple[str, ...],
) -> EvalCase | None:
    rewrite = row.get("gold_rewrite")
    if not isinstance(rewrite, str) or not rewrite.strip():
        return None
    bundle_id = context.bundle.manifest.bundle_id
    local_id = str(row["question_id"])
    return EvalCase(
        case_id=namespace_id(bundle_id, f"rewrite:{local_id}"),
        family_id=namespace_id(bundle_id, f"scenario:{row['scenario_group']}"),
        evaluation_scope="full_corpus",
        provenance="synthetic",
        tags=_tags(bundle_id, row),
        source_row_ids=_qa_source_rows(bundle_id, row, seed_local_ids),
        inputs=RewriteInput(
            current_query=str(row["question"]),
            recent_messages=(),
            long_term_memories=seeds,
        ),
        gold=_qa_gold(bundle_id, row),
        review=context.bundle.manifest.review,
    )


def cross_session_source_sha256(inputs: CrossSessionInput) -> str:
    """Bind a shared source fixture to its exact transcript and logical owner."""
    from evaluation.scoring import output_sha256

    return output_sha256(
        {
            "logical_user_id": inputs.user_id,
            "session_a": inputs.session_a,
            "messages": [message.model_dump(mode="json") for message in inputs.session_a_messages],
        }
    )


def _cross_session_case(context: _BundleContext, row: Mapping[str, Any]) -> EvalCase:
    bundle_id = context.bundle.manifest.bundle_id
    local_id = str(row["question_id"])
    memories = _document_rows(
        context.bundle.memories,
        "memory_gold",
        label=f"{bundle_id}/memory_gold",
    )
    memories_by_id = {str(memory["memory_id"]): memory for memory in memories}
    active_memory_ids = [
        str(value)
        for value in context.bundle.memories.get("expected_active_memory_row_source_event_ids", [])
    ]
    evidence_ids = [str(value) for value in row["evidence_turn_ids"]]
    for memory_id in active_memory_ids:
        try:
            memory = memories_by_id[memory_id]
        except KeyError as error:
            raise ValueError(f"unknown active memory row: {bundle_id}:{memory_id}") from error
        evidence_ids.extend(str(value) for value in memory["source_turn_ids"])
        evidence_ids.extend(str(value) for value in memory.get("supporting_turn_ids", []))
    # QA gold is the end-of-bundle corpus, so replay the original complete trajectory rather
    # than concatenate chosen active assertions. Superseded/cancelled events and their context
    # must still reach native formation, in their own source conversations.
    _evidence_window(context, evidence_ids)  # Validate references before expanding the trajectory.
    messages, pending, message_rows = _evidence_window(
        context, (message.local_message_id for message in context.messages)
    )
    relevant_local_ids = tuple(
        dict.fromkeys(
            [
                *[str(value) for value in row.get("required_memory_ids", [])],
                *[str(value) for value in row.get("supporting_memory_ids", [])],
                *[str(value) for value in row.get("history_memory_ids", [])],
            ]
        )
    )
    reasons: list[str] = []
    if pending:
        reasons.append("pending_kira_assistant")
    if row.get("final_answer") == _PENDING_ANSWER:
        reasons.append("pending_kira_final_answer")
    source_rows = [
        *message_rows,
        _source_row_id(bundle_id, "qa", local_id),
        *(
            _source_row_id(bundle_id, "memory", memory_id)
            for memory_id in dict.fromkeys((*active_memory_ids, *relevant_local_ids))
        ),
    ]
    # Native cross-session mapping must not depend on also selecting the formation suite.
    # These are judge/scoring references only; Session A still forms its entire corpus natively.
    gold = _qa_gold(bundle_id, row).model_copy(
        update={
            "facts": tuple(
                GoldFact(
                    gold_id=namespace_id(bundle_id, memory_id),
                    text=str(memories_by_id[memory_id]["canonical_fact"]),
                    evidence_message_ids=tuple(
                        namespace_id(bundle_id, str(source_id))
                        for source_id in dict.fromkeys(
                            (
                                *memories_by_id[memory_id]["source_turn_ids"],
                                *memories_by_id[memory_id].get("supporting_turn_ids", []),
                            )
                        )
                    ),
                    attributed_to="user",
                )
                for memory_id in relevant_local_ids
                if memory_id in memories_by_id
            )
        }
    )
    return EvalCase(
        case_id=namespace_id(bundle_id, f"cross-session:{local_id}"),
        family_id=namespace_id(bundle_id, f"scenario:{row['scenario_group']}"),
        evaluation_scope="full_corpus",
        provenance="synthetic",
        tags=_tags(bundle_id, row),
        source_row_ids=tuple(dict.fromkeys(source_rows)),
        eligibility=_eligibility(*reasons),
        inputs=CrossSessionInput(
            user_id=context.user_id,
            session_a=namespace_id(bundle_id, "source-evidence"),
            session_b=namespace_id(bundle_id, f"qa-{local_id}"),
            session_a_messages=messages,
            session_b_query=str(row["question"]),
            source_session_ids=tuple(
                dict.fromkeys(
                    message.session_id for message in messages if message.session_id is not None
                )
            ),
        ),
        gold=gold,
        review=context.bundle.manifest.review,
    )


def _active_seed_memories(
    context: _BundleContext,
    memories: Sequence[Mapping[str, Any]],
) -> tuple[tuple[SeedMemory, ...], tuple[str, ...]]:
    bundle_id = context.bundle.manifest.bundle_id
    by_id = {str(memory["memory_id"]): memory for memory in memories}
    local_ids = tuple(
        str(value)
        for value in context.bundle.memories.get("expected_active_memory_row_source_event_ids", [])
    )
    seeds: list[SeedMemory] = []
    for local_id in local_ids:
        try:
            memory = by_id[local_id]
        except KeyError as error:
            raise ValueError(f"unknown active memory row: {bundle_id}:{local_id}") from error
        seeds.append(
            SeedMemory(
                gold_id=namespace_id(bundle_id, local_id),
                user_id=context.user_id,
                text=str(memory["canonical_fact"]),
            )
        )
    return tuple(seeds), local_ids


def _case_order(case: EvalCase, seed: int) -> tuple[str, str]:
    digest = _sha256(f"{seed}\0{case.case_id}".encode())
    return digest, case.case_id


def _register_source(
    rows: dict[str, _MutableCoverage],
    row_id: str,
    kind: SourceKind,
    *roles: str,
) -> None:
    if row_id in rows:
        raise ValueError(f"duplicate source row ID: {row_id}")
    rows[row_id] = _MutableCoverage(kind=kind, roles=set(roles), case_ids=set())


def _source_coverage(
    contexts: Sequence[_BundleContext],
    cases: Sequence[EvalCase],
) -> tuple[SourceCoverageRow, ...]:
    rows: dict[str, _MutableCoverage] = {}
    for context in contexts:
        bundle_id = context.bundle.manifest.bundle_id
        for message in context.messages:
            _register_source(
                rows,
                _source_row_id(bundle_id, "conversation", message.local_message_id),
                "conversation",
                "ingestion_source",
            )
        for fill in _document_rows(context.bundle.fills, "fills", label=f"{bundle_id}/fills"):
            _register_source(
                rows,
                _source_row_id(bundle_id, "fill", str(fill["fill_id"])),
                "fill",
                "materialization_contract",
            )
        active = set(
            str(value)
            for value in context.bundle.memories.get(
                "expected_active_memory_row_source_event_ids", []
            )
        )
        for memory in _document_rows(
            context.bundle.memories,
            "memory_gold",
            label=f"{bundle_id}/memory_gold",
        ):
            local_id = str(memory["memory_id"])
            roles = ["gold_specification"]
            if local_id in active:
                roles.append("retrieval_seed")
            _register_source(
                rows,
                _source_row_id(bundle_id, "memory", local_id),
                "memory",
                *roles,
            )
        for qa in _document_rows(context.bundle.qa, "qa", label=f"{bundle_id}/qa"):
            _register_source(
                rows,
                _source_row_id(bundle_id, "qa", str(qa["question_id"])),
                "qa",
                "query_and_gold",
            )

    for case in cases:
        for row_id in case.source_row_ids:
            try:
                rows[row_id].case_ids.add(case.case_id)
            except KeyError as error:
                raise ValueError(f"case references unknown source row: {row_id}") from error

    result: list[SourceCoverageRow] = []
    for row_id, row in sorted(rows.items(), key=lambda item: (item[1].kind, item[0])):
        linked = bool(row.case_ids)
        result.append(
            SourceCoverageRow(
                row_id=row_id,
                kind=row.kind,
                roles=tuple(sorted(row.roles)),
                case_ids=tuple(sorted(row.case_ids)),
                state="linked" if linked else "accounted_not_selected",
                reason=None if linked else "not_referenced_by_compiled_case",
            )
        )
    return tuple(result)


def _coverage(
    cases: Sequence[EvalCase],
    rows: tuple[SourceCoverageRow, ...],
) -> CompilationCoverage:
    suites: list[SuiteCoverage] = []
    for suite in Suite:
        selected = [case for case in cases if case.suite is suite]
        eligible = sum(case.eligibility.status == "eligible" for case in selected)
        suites.append(
            SuiteCoverage(
                suite=suite,
                total=len(selected),
                eligible=eligible,
                blocked=len(selected) - eligible,
            )
        )

    sources: list[SourceCoverage] = []
    for kind in ("conversation", "fill", "memory", "qa"):
        selected_rows = [row for row in rows if row.kind == kind]
        linked = sum(row.state == "linked" for row in selected_rows)
        sources.append(
            SourceCoverage(
                kind=kind,
                total=len(selected_rows),
                linked=linked,
                accounted_not_selected=len(selected_rows) - linked,
            )
        )

    reasons = Counter(reason for case in cases for reason in case.eligibility.blocked_reasons)
    return CompilationCoverage(
        suites=tuple(suites),
        sources=tuple(sources),
        blocked_reasons=tuple(
            BlockedReasonCoverage(reason=reason, cases=count)
            for reason, count in sorted(reasons.items())
        ),
        rows=rows,
    )


def compile_dataset(
    root: Path | None = None,
    *,
    seed: int = 0,
) -> DatasetCompilation:
    """Compile all canonical rows and retain explicit coverage for every source row."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or seed > 2**63 - 1:
        raise ValueError("seed must be an integer between 0 and 2^63-1")
    dataset_root = (root or default_dataset_root()).resolve()
    manifest = load_manifest(dataset_root)
    dataset_sha256 = _dataset_fingerprint(dataset_root, manifest)
    contexts = tuple(
        _bundle_context(load_bundle(entry, root=dataset_root)) for entry in manifest.bundles
    )

    cases: list[EvalCase] = []
    for context in contexts:
        memories = _document_rows(
            context.bundle.memories,
            "memory_gold",
            label=f"{context.bundle.manifest.bundle_id}/memory_gold",
        )
        qa_rows = _document_rows(
            context.bundle.qa,
            "qa",
            label=f"{context.bundle.manifest.bundle_id}/qa",
        )
        seeds, seed_local_ids = _active_seed_memories(context, memories)
        cases.extend(_formation_case(context, memory) for memory in memories)
        for row in qa_rows:
            cases.append(_retrieval_case(context, row, seeds, seed_local_ids))
            if rewrite := _rewrite_case(context, row, seeds, seed_local_ids):
                cases.append(rewrite)
            cases.append(_cross_session_case(context, row))

    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        duplicate = next(case_id for case_id, count in Counter(ids).items() if count > 1)
        raise ValueError(f"duplicate compiled case ID: {duplicate}")
    ordered = tuple(sorted(cases, key=lambda case: _case_order(case, seed)))
    rows = _source_coverage(contexts, ordered)
    return DatasetCompilation(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        dataset_sha256=dataset_sha256,
        seed=seed,
        cases=ordered,
        coverage=_coverage(ordered, rows),
    )
