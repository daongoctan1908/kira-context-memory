"""Run-scoped identities and cleanup authorization for persistent benchmark trials.

This module allocates resources but does not create or delete database objects.  Persistent
evaluators must claim the plan in their database boundary before executing its cleanup scope.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID, uuid5

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, model_validator

from app.infrastructure.memory.postgres_admin import normalize_psycopg_dsn
from evaluation.config import EvalConfig
from evaluation.models import EvalModel, Identifier, Sha256


class IsolationPlan(EvalModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    owner_token: UUID
    database_mode: Literal["dedicated_disposable"] = "dedicated_disposable"
    worker_mode: Literal["stopped"] = "stopped"
    conversation_database_sha256: Sha256
    memory_database_sha256: Sha256
    conversation_schema: Literal["public"] = "public"
    memory_schema: str = Field(pattern=r"^eval_[a-f0-9]{12}$")
    memory_collection: str = Field(pattern=r"^mem_[a-f0-9]{12}$")
    user_namespace: str = Field(pattern=r"^eval:[a-f0-9]{12}$")

    @model_validator(mode="after")
    def names_belong_to_run(self) -> "IsolationPlan":
        suffix = self.run_id.hex[:12]
        if (
            self.memory_schema != f"eval_{suffix}"
            or self.memory_collection != f"mem_{suffix}"
            or self.user_namespace != f"eval:{suffix}"
        ):
            raise ValueError("isolation resource names must be derived from the run ID")
        return self


class CaseResourceOwnership(EvalModel):
    case_id: Identifier
    attempt: int = Field(ge=1, strict=True)
    resource_role: Literal["case", "bundle_source", "bundle_qa"] = "case"
    bundle_id: Identifier | None = None
    user_id: Identifier
    session_id: Identifier
    conversation_id: UUID
    event_id: UUID
    memory_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def memory_ids_are_unique(self) -> "CaseResourceOwnership":
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("owned memory IDs must be unique")
        if (self.resource_role == "case") != (self.bundle_id is None):
            raise ValueError("bundle resources require exactly one bundle identity")
        if self.resource_role == "bundle_source" and self.attempt != 1:
            raise ValueError("bundle source has one stable formation attempt")
        if self.resource_role == "bundle_qa" and self.memory_ids:
            raise ValueError("QA resources cannot own source memory")
        return self


class IsolationLedger(EvalModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    owner_token: UUID
    plan_sha256: Sha256
    resources: tuple[CaseResourceOwnership, ...] = ()

    @model_validator(mode="after")
    def resources_do_not_overlap(self) -> "IsolationLedger":
        identities = [(resource.case_id, resource.attempt) for resource in self.resources]
        if len(identities) != len(set(identities)):
            raise ValueError("case-attempt ownership entries must be unique")
        for field in ("session_id", "conversation_id", "event_id"):
            values = [getattr(resource, field) for resource in self.resources]
            if len(values) != len(set(values)):
                raise ValueError(f"owned {field} values must be unique")
        by_user: dict[str, list[CaseResourceOwnership]] = {}
        sources: dict[str, CaseResourceOwnership] = {}
        for resource in self.resources:
            by_user.setdefault(resource.user_id, []).append(resource)
            if resource.resource_role == "bundle_source":
                assert resource.bundle_id is not None
                if resource.bundle_id in sources:
                    raise ValueError("bundle has more than one source owner")
                sources[resource.bundle_id] = resource
        for resources in by_user.values():
            if len(resources) > 1 and (
                any(item.bundle_id is None for item in resources)
                or len({item.bundle_id for item in resources}) != 1
            ):
                raise ValueError("shared user IDs must belong to one source bundle")
        for resource in self.resources:
            if resource.resource_role == "bundle_qa":
                source = sources.get(resource.bundle_id or "")
                if source is None or source.user_id != resource.user_id:
                    raise ValueError("QA user must be owned by its source bundle")
        memory_ids = [memory_id for resource in self.resources for memory_id in resource.memory_ids]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("memory IDs cannot be owned by multiple case attempts")
        return self


class CleanupScope(EvalModel):
    """Only these exact resources may be touched by persistent-suite cleanup."""

    run_id: UUID
    owner_token: UUID
    plan_sha256: Sha256
    conversation_database_sha256: Sha256
    memory_database_sha256: Sha256
    conversation_schema: Literal["public"] = "public"
    memory_schema: str = Field(pattern=r"^eval_[a-f0-9]{12}$")
    memory_collection: str = Field(pattern=r"^mem_[a-f0-9]{12}$")
    user_ids: tuple[Identifier, ...]
    session_ids: tuple[Identifier, ...]
    conversation_ids: tuple[UUID, ...]
    event_ids: tuple[UUID, ...]
    memory_ids: tuple[UUID, ...]
    drop_owned_memory_schema: Literal[True] = True


def database_fingerprint(database_url: SecretStr | str) -> str:
    """Fingerprint database identity without hashing credentials into benchmark artifacts."""

    raw = database_url.get_secret_value() if isinstance(database_url, SecretStr) else database_url
    try:
        values = conninfo_to_dict(normalize_psycopg_dsn(raw))
    except Exception:
        raise ValueError("invalid PostgreSQL database identity") from None
    safe = {
        key: values.get(key, "")
        for key in ("host", "hostaddr", "port", "dbname", "user", "sslmode")
    }
    canonical = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def create_isolation_plan(
    *,
    run_id: UUID,
    owner_token: UUID,
    conversation_database_url: SecretStr | str,
    memory_database_url: SecretStr | str,
) -> IsolationPlan:
    suffix = run_id.hex[:12]
    return IsolationPlan(
        run_id=run_id,
        owner_token=owner_token,
        conversation_database_sha256=database_fingerprint(conversation_database_url),
        memory_database_sha256=database_fingerprint(memory_database_url),
        memory_schema=f"eval_{suffix}",
        memory_collection=f"mem_{suffix}",
        user_namespace=f"eval:{suffix}",
    )


def isolation_plan_sha256(plan: IsolationPlan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def apply_isolation(config: EvalConfig, plan: IsolationPlan) -> EvalConfig:
    """Bind eval configuration to the exact disposable databases named by the plan."""

    if config.database_url is None or config.memory_database_url is None:
        raise ValueError("persistent evaluation requires both PostgreSQL database URLs")
    if database_fingerprint(config.database_url) != plan.conversation_database_sha256:
        raise ValueError("conversation database does not match the isolation plan")
    if database_fingerprint(config.memory_database_url) != plan.memory_database_sha256:
        raise ValueError("memory database does not match the isolation plan")
    return config.model_copy(
        update={
            "memory_schema": plan.memory_schema,
            "memory_collection": plan.memory_collection,
        }
    )


def new_isolation_ledger(plan: IsolationPlan) -> IsolationLedger:
    return IsolationLedger(
        run_id=plan.run_id,
        owner_token=plan.owner_token,
        plan_sha256=isolation_plan_sha256(plan),
    )


def _resource_digest(plan: IsolationPlan, case_id: str, attempt: int) -> str:
    value = f"{plan.owner_token}\0{case_id}\0{attempt}".encode()
    return hashlib.sha256(value).hexdigest()[:16]


def kira_benchmark_username(plan: IsolationPlan, *, case_id: str, attempt: int, arm: str) -> str:
    """Fresh server-side context per case/arm/attempt with no user-provided text in the name."""

    if attempt < 1 or isinstance(attempt, bool) or arm not in {"no_ltm", "with_ltm"}:
        raise ValueError("KiRa isolation requires a valid case attempt and arm")
    value = f"{plan.run_id}\0{plan.owner_token}\0{case_id}\0{attempt}\0{arm}".encode()
    return f"bench_{hashlib.sha256(value).hexdigest()[:32]}"


def allocate_case_resources(
    plan: IsolationPlan,
    *,
    case_id: str,
    attempt: int,
) -> CaseResourceOwnership:
    """Allocate retry-stable IDs; a new attempt always receives fresh isolated state."""

    if attempt < 1 or isinstance(attempt, bool):
        raise ValueError("case attempt must be a positive integer")
    digest = _resource_digest(plan, case_id, attempt)
    return CaseResourceOwnership(
        case_id=case_id,
        attempt=attempt,
        user_id=f"{plan.user_namespace}:u:{digest}",
        session_id=f"{plan.user_namespace}:s:{digest}",
        conversation_id=uuid5(plan.run_id, f"{plan.owner_token}:conversation:{case_id}:{attempt}"),
        event_id=uuid5(plan.run_id, f"{plan.owner_token}:event:{case_id}:{attempt}"),
    )


def allocate_bundle_resources(plan: IsolationPlan, *, bundle_id: str) -> CaseResourceOwnership:
    """One durable user/source conversation owner for a dataset bundle in this run."""

    case_id = f"bundle:{bundle_id}:source"
    allocated = allocate_case_resources(plan, case_id=case_id, attempt=1)
    return allocated.model_copy(update={"resource_role": "bundle_source", "bundle_id": bundle_id})


def allocate_bundle_qa_resources(
    plan: IsolationPlan,
    *,
    bundle_id: str,
    case_id: str,
    attempt: int,
) -> CaseResourceOwnership:
    """Fresh QA context using the already-formed bundle's user, without owning its memory."""

    allocated = allocate_case_resources(plan, case_id=case_id, attempt=attempt)
    source = allocate_bundle_resources(plan, bundle_id=bundle_id)
    return allocated.model_copy(
        update={"resource_role": "bundle_qa", "bundle_id": bundle_id, "user_id": source.user_id}
    )


def allocated_resource(
    plan: IsolationPlan, resource: CaseResourceOwnership
) -> CaseResourceOwnership:
    """Reconstruct the deterministic pre-write ownership record, including shared source users."""

    if resource.resource_role == "bundle_source":
        assert resource.bundle_id is not None
        return allocate_bundle_resources(plan, bundle_id=resource.bundle_id)
    if resource.resource_role == "bundle_qa":
        assert resource.bundle_id is not None
        return allocate_bundle_qa_resources(
            plan, bundle_id=resource.bundle_id, case_id=resource.case_id, attempt=resource.attempt
        )
    return allocate_case_resources(plan, case_id=resource.case_id, attempt=resource.attempt)


def claim_case_resources(
    plan: IsolationPlan,
    *,
    case_id: str,
    attempt: int,
    conversation_id: UUID,
    event_id: UUID,
) -> CaseResourceOwnership:
    """Bind database-generated UUIDs to the deterministic run-owned user/session scope."""

    allocated = allocate_case_resources(plan, case_id=case_id, attempt=attempt)
    if not isinstance(conversation_id, UUID) or not isinstance(event_id, UUID):
        raise ValueError("observed conversation and event IDs must be UUIDs")
    return allocated.model_copy(update={"conversation_id": conversation_id, "event_id": event_id})


def register_case_resources(
    ledger: IsolationLedger,
    plan: IsolationPlan,
    resource: CaseResourceOwnership,
) -> IsolationLedger:
    _validate_ledger_owner(ledger, plan)
    expected = allocated_resource(plan, resource)
    if (
        resource.case_id != expected.case_id
        or resource.attempt != expected.attempt
        or resource.user_id != expected.user_id
        or resource.session_id != expected.session_id
        or resource.resource_role != expected.resource_role
        or resource.bundle_id != expected.bundle_id
    ):
        raise ValueError("case resources were not allocated by this isolation plan")
    existing = next(
        (
            item
            for item in ledger.resources
            if (item.case_id, item.attempt) == (resource.case_id, resource.attempt)
        ),
        None,
    )
    if existing is not None:
        if existing != resource:
            # The deterministic allocation is durably recorded before the first database write.
            # PostgreSQL then supplies the real conversation/job UUIDs exactly once.  Once either
            # observed UUID has been claimed, no later retry may reassign the ownership record.
            if existing != expected or existing.memory_ids:
                raise ValueError("case-attempt resources cannot be reassigned")
            resources = tuple(resource if item == existing else item for item in ledger.resources)
            return ledger.model_copy(update={"resources": resources})
        return ledger
    return IsolationLedger(
        run_id=ledger.run_id,
        owner_token=ledger.owner_token,
        plan_sha256=ledger.plan_sha256,
        resources=(*ledger.resources, resource),
    )


def register_memory_ids(
    ledger: IsolationLedger,
    plan: IsolationPlan,
    *,
    case_id: str,
    attempt: int,
    memory_ids: Sequence[UUID],
) -> IsolationLedger:
    _validate_ledger_owner(ledger, plan)
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("registered memory IDs must be unique")
    resources = list(ledger.resources)
    index = next(
        (
            position
            for position, resource in enumerate(resources)
            if (resource.case_id, resource.attempt) == (case_id, attempt)
        ),
        None,
    )
    if index is None:
        raise ValueError("case resources must be registered before memory IDs")
    owned_elsewhere = {
        memory_id
        for position, resource in enumerate(resources)
        if position != index
        for memory_id in resource.memory_ids
    }
    if owned_elsewhere.intersection(memory_ids):
        raise ValueError("memory ID is already owned by another case attempt")
    existing_memory_ids = resources[index].memory_ids
    if resources[index].resource_role == "bundle_qa" and memory_ids:
        raise ValueError("QA resources cannot own source memory")
    if not set(existing_memory_ids).issubset(memory_ids):
        raise ValueError("registered memory ownership cannot be removed")
    combined = (
        *existing_memory_ids,
        *(item for item in memory_ids if item not in existing_memory_ids),
    )
    resources[index] = resources[index].model_copy(update={"memory_ids": combined})
    return IsolationLedger(
        run_id=ledger.run_id,
        owner_token=ledger.owner_token,
        plan_sha256=ledger.plan_sha256,
        resources=tuple(resources),
    )


def authorize_cleanup(
    plan: IsolationPlan,
    ledger: IsolationLedger,
    *,
    observed_owner_token: UUID,
    observed_plan_sha256: str,
    workers_stopped: bool,
) -> CleanupScope:
    """Fail closed unless the DB marker and stopped-worker assertion match this exact run."""

    _validate_ledger_owner(ledger, plan)
    if not workers_stopped:
        raise ValueError("cleanup requires all benchmark workers to be stopped")
    if observed_owner_token != plan.owner_token:
        raise ValueError("database ownership marker belongs to another run")
    if observed_plan_sha256 != isolation_plan_sha256(plan):
        raise ValueError("database ownership marker does not match the isolation plan")
    ordered = sorted(ledger.resources, key=lambda item: (item.case_id, item.attempt))
    return CleanupScope(
        run_id=plan.run_id,
        owner_token=plan.owner_token,
        plan_sha256=ledger.plan_sha256,
        conversation_database_sha256=plan.conversation_database_sha256,
        memory_database_sha256=plan.memory_database_sha256,
        memory_schema=plan.memory_schema,
        memory_collection=plan.memory_collection,
        user_ids=tuple(dict.fromkeys(item.user_id for item in ordered)),
        session_ids=tuple(item.session_id for item in ordered),
        conversation_ids=tuple(item.conversation_id for item in ordered),
        event_ids=tuple(item.event_id for item in ordered),
        memory_ids=tuple(memory_id for item in ordered for memory_id in item.memory_ids),
    )


def _validate_ledger_owner(ledger: IsolationLedger, plan: IsolationPlan) -> None:
    if (
        ledger.run_id != plan.run_id
        or ledger.owner_token != plan.owner_token
        or ledger.plan_sha256 != isolation_plan_sha256(plan)
    ):
        raise ValueError("resource ledger does not belong to the isolation plan")
