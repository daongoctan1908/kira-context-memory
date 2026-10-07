"""Run-scoped benchmark resource identity and cleanup authorization tests."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from evaluation.config import EvalConfig
from evaluation.isolation import (
    IsolationPlan,
    allocate_bundle_qa_resources,
    allocate_bundle_resources,
    allocate_case_resources,
    apply_isolation,
    authorize_cleanup,
    claim_case_resources,
    create_isolation_plan,
    database_fingerprint,
    isolation_plan_sha256,
    kira_benchmark_username,
    new_isolation_ledger,
    register_case_resources,
    register_memory_ids,
)

_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_OWNER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_CONVERSATION_URL = "postgresql://eval:conversation-secret@localhost:15433/kira_eval"
_MEMORY_URL = "postgresql+asyncpg://eval:memory-secret@localhost:15433/kira_eval"


def _plan() -> IsolationPlan:
    return create_isolation_plan(
        run_id=_RUN_ID,
        owner_token=_OWNER,
        conversation_database_url=_CONVERSATION_URL,
        memory_database_url=_MEMORY_URL,
    )


def test_plan_uses_run_scoped_names_and_credential_free_database_fingerprints():
    plan = _plan()

    assert plan.memory_schema == "eval_aaaaaaaaaaaa"
    assert plan.memory_collection == "mem_aaaaaaaaaaaa"
    assert plan.user_namespace == "eval:aaaaaaaaaaaa"
    assert plan.database_mode == "dedicated_disposable"
    assert plan.worker_mode == "stopped"
    assert database_fingerprint(_CONVERSATION_URL) == database_fingerprint(
        "postgresql://eval:different-password@localhost:15433/kira_eval"
    )
    assert database_fingerprint(_CONVERSATION_URL) != database_fingerprint(
        "postgresql://eval:conversation-secret@localhost:15433/live_application"
    )
    serialized = plan.model_dump_json()
    assert "conversation-secret" not in serialized
    assert "memory-secret" not in serialized


def test_kira_identity_is_stable_within_attempt_and_distinct_per_case_arm_attempt():
    plan = _plan()
    first = kira_benchmark_username(plan, case_id="case-1", attempt=1, arm="no_ltm")
    assert first == kira_benchmark_username(plan, case_id="case-1", attempt=1, arm="no_ltm")
    variants = (
        kira_benchmark_username(plan, case_id="case-1", attempt=1, arm="with_ltm"),
        kira_benchmark_username(plan, case_id="case-1", attempt=2, arm="no_ltm"),
        kira_benchmark_username(plan, case_id="case-2", attempt=1, arm="no_ltm"),
    )
    assert len({first, *variants}) == 4
    assert "case-1" not in first
    assert len(first) == 38
    with pytest.raises(ValueError, match="valid case"):
        kira_benchmark_username(plan, case_id="case-1", attempt=0, arm="no_ltm")
    with pytest.raises(ValueError, match="valid case"):
        kira_benchmark_username(plan, case_id="case-1", attempt=1, arm="invalid")


def test_plan_rejects_names_not_derived_from_run_and_invalid_database_is_sanitized():
    data = _plan().model_dump()
    data["memory_schema"] = "eval_deadbeefdead"
    with pytest.raises(ValidationError, match="derived from the run"):
        IsolationPlan.model_validate(data)
    with pytest.raises(ValueError, match="invalid PostgreSQL") as captured:
        database_fingerprint("sqlite:///secret")
    assert "secret" not in str(captured.value)


def test_apply_isolation_binds_only_matching_disposable_databases():
    config = EvalConfig(
        database_url=_CONVERSATION_URL,
        memory_database_url=_MEMORY_URL,
        memory_schema="memory",
        memory_collection="memories",
    )
    isolated = apply_isolation(config, _plan())

    assert isolated.memory_schema == "eval_aaaaaaaaaaaa"
    assert isolated.memory_collection == "mem_aaaaaaaaaaaa"
    assert isolated.database_url == config.database_url
    assert isolated.memory_database_url == config.memory_database_url

    wrong = config.model_copy(
        update={
            "database_url": type(config.database_url)(
                "postgresql://eval:secret@localhost:15433/another_database"
            )
        }
    )
    with pytest.raises(ValueError, match="conversation database"):
        apply_isolation(wrong, _plan())
    with pytest.raises(ValueError, match="requires both"):
        apply_isolation(EvalConfig(), _plan())


def test_case_resource_allocation_is_retry_stable_and_attempt_isolated():
    plan = _plan()
    first = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=1)
    same = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=1)
    retry = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=2)

    assert first == same
    assert first.user_id.startswith("eval:aaaaaaaaaaaa:u:")
    assert first.session_id.startswith("eval:aaaaaaaaaaaa:s:")
    assert first.user_id != retry.user_id
    assert first.session_id != retry.session_id
    assert first.conversation_id != retry.conversation_id
    assert first.event_id != retry.event_id
    with pytest.raises(ValueError, match="positive integer"):
        allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=0)


def test_ledger_allocation_can_be_claimed_once_but_cannot_be_reassigned():
    plan = _plan()
    ledger = new_isolation_ledger(plan)
    resource = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=1)

    registered = register_case_resources(ledger, plan, resource)
    assert register_case_resources(registered, plan, resource) == registered
    observed = claim_case_resources(
        plan,
        case_id=resource.case_id,
        attempt=resource.attempt,
        conversation_id=UUID(int=122),
        event_id=UUID(int=123),
    )
    assert observed.user_id == resource.user_id
    assert observed.session_id == resource.session_id
    claimed = register_case_resources(registered, plan, observed)
    assert claimed.resources == (observed,)
    reassigned = observed.model_copy(update={"event_id": UUID(int=124)})
    with pytest.raises(ValueError, match="cannot be reassigned"):
        register_case_resources(claimed, plan, reassigned)

    forged = resource.model_copy(update={"user_id": "eval:aaaaaaaaaaaa:u:forged"})
    with pytest.raises(ValueError, match="not allocated"):
        register_case_resources(registered, plan, forged)

    other_plan = create_isolation_plan(
        run_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        owner_token=_OWNER,
        conversation_database_url=_CONVERSATION_URL,
        memory_database_url=_MEMORY_URL,
    )
    with pytest.raises(ValueError, match="does not belong"):
        register_case_resources(registered, other_plan, resource)


def test_memory_ids_must_follow_case_registration_and_cannot_cross_owners():
    plan = _plan()
    first = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=1)
    second = allocate_case_resources(plan, case_id="conv01:formation:M02", attempt=1)
    ledger = register_case_resources(new_isolation_ledger(plan), plan, first)
    memory_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

    with pytest.raises(ValueError, match="before memory"):
        register_memory_ids(
            ledger,
            plan,
            case_id=second.case_id,
            attempt=second.attempt,
            memory_ids=(memory_id,),
        )
    ledger = register_memory_ids(
        ledger,
        plan,
        case_id=first.case_id,
        attempt=first.attempt,
        memory_ids=(memory_id,),
    )
    with pytest.raises(ValueError, match="cannot be removed"):
        register_memory_ids(
            ledger,
            plan,
            case_id=first.case_id,
            attempt=first.attempt,
            memory_ids=(),
        )
    ledger = register_case_resources(ledger, plan, second)
    with pytest.raises(ValueError, match="another case"):
        register_memory_ids(
            ledger,
            plan,
            case_id=second.case_id,
            attempt=second.attempt,
            memory_ids=(memory_id,),
        )


def test_cleanup_is_exact_and_requires_matching_db_marker_and_stopped_workers():
    plan = _plan()
    first = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=1)
    second = allocate_case_resources(plan, case_id="conv01:formation:M01", attempt=2)
    ledger = register_case_resources(new_isolation_ledger(plan), plan, first)
    ledger = register_case_resources(ledger, plan, second)
    memories = (
        UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
    )
    ledger = register_memory_ids(
        ledger,
        plan,
        case_id=first.case_id,
        attempt=first.attempt,
        memory_ids=memories,
    )

    with pytest.raises(ValueError, match="workers"):
        authorize_cleanup(
            plan,
            ledger,
            observed_owner_token=_OWNER,
            observed_plan_sha256=isolation_plan_sha256(plan),
            workers_stopped=False,
        )
    with pytest.raises(ValueError, match="another run"):
        authorize_cleanup(
            plan,
            ledger,
            observed_owner_token=UUID(int=999),
            observed_plan_sha256=isolation_plan_sha256(plan),
            workers_stopped=True,
        )
    with pytest.raises(ValueError, match="marker"):
        authorize_cleanup(
            plan,
            ledger,
            observed_owner_token=_OWNER,
            observed_plan_sha256="0" * 64,
            workers_stopped=True,
        )

    scope = authorize_cleanup(
        plan,
        ledger,
        observed_owner_token=_OWNER,
        observed_plan_sha256=isolation_plan_sha256(plan),
        workers_stopped=True,
    )
    assert scope.memory_schema == plan.memory_schema
    assert scope.drop_owned_memory_schema is True
    assert scope.user_ids == (first.user_id, second.user_id)
    assert scope.conversation_ids == (first.conversation_id, second.conversation_id)
    assert scope.event_ids == (first.event_id, second.event_id)
    assert scope.memory_ids == memories
    assert not hasattr(scope, "user_prefix")


def test_bundles_share_storage_but_keep_four_source_users_and_qa_contexts_isolated():
    plan = _plan()
    ledger = new_isolation_ledger(plan)
    sources = []
    for bundle_id in ("conv01", "conv02", "conv03", "conv04"):
        source = allocate_bundle_resources(plan, bundle_id=bundle_id)
        sources.append(source)
        ledger = register_case_resources(ledger, plan, source)
        assert allocate_bundle_resources(plan, bundle_id=bundle_id) == source
        for index in (1, 2):
            qa = allocate_bundle_qa_resources(
                plan, bundle_id=bundle_id, case_id=f"{bundle_id}:Q{index}", attempt=1
            )
            assert qa.user_id == source.user_id
            assert qa.session_id != source.session_id
            assert qa.conversation_id != source.conversation_id
            ledger = register_case_resources(ledger, plan, qa)
    assert len({source.user_id for source in sources}) == 4
    assert len({resource.session_id for resource in ledger.resources}) == 12
    scope = authorize_cleanup(
        plan,
        ledger,
        observed_owner_token=plan.owner_token,
        observed_plan_sha256=isolation_plan_sha256(plan),
        workers_stopped=True,
    )
    assert len(scope.user_ids) == 4
    assert len(scope.conversation_ids) == 12


def test_qa_retries_keep_source_user_but_cannot_claim_or_remove_source_memory():
    plan = _plan()
    source = allocate_bundle_resources(plan, bundle_id="conv01")
    ledger = register_case_resources(new_isolation_ledger(plan), plan, source)
    first = allocate_bundle_qa_resources(plan, bundle_id="conv01", case_id="conv01:Q1", attempt=1)
    retry = allocate_bundle_qa_resources(plan, bundle_id="conv01", case_id="conv01:Q1", attempt=2)
    assert first.user_id == retry.user_id == source.user_id
    assert first.session_id != retry.session_id
    memory_id = UUID(int=700)
    ledger = register_memory_ids(
        ledger, plan, case_id=source.case_id, attempt=1, memory_ids=(memory_id,)
    )
    ledger = register_case_resources(ledger, plan, first)
    ledger = register_case_resources(ledger, plan, retry)
    with pytest.raises(ValueError, match="QA resources cannot own"):
        register_memory_ids(
            ledger, plan, case_id=first.case_id, attempt=1, memory_ids=(UUID(int=701),)
        )
    with pytest.raises(ValueError, match="cannot be removed"):
        register_memory_ids(ledger, plan, case_id=source.case_id, attempt=1, memory_ids=())
    forged = retry.model_copy(
        update={"user_id": allocate_bundle_resources(plan, bundle_id="conv02").user_id}
    )
    with pytest.raises(ValueError, match="not allocated"):
        register_case_resources(ledger, plan, forged)


def test_bundle_source_ownership_can_be_claimed_once_without_changing_shared_user():
    plan = _plan()
    source = allocate_bundle_resources(plan, bundle_id="conv01")
    ledger = register_case_resources(new_isolation_ledger(plan), plan, source)
    claimed = source.model_copy(
        update={"conversation_id": UUID(int=900), "event_id": UUID(int=901)}
    )
    ledger = register_case_resources(ledger, plan, claimed)
    assert ledger.resources[0] == claimed
    with pytest.raises(ValueError, match="cannot be reassigned"):
        register_case_resources(
            ledger, plan, claimed.model_copy(update={"event_id": UUID(int=902)})
        )
    qa = allocate_bundle_qa_resources(plan, bundle_id="conv01", case_id="conv01:Q1", attempt=1)
    with pytest.raises(ValueError, match="source bundle"):
        register_case_resources(new_isolation_ledger(plan), plan, qa)
