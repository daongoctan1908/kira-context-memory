"""PC acceptance is a technical gate, never a candidate promotion scorer."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.artifacts import (
    ArtifactRunIdentity,
    ArtifactStore,
    BundleSourceArtifact,
    BundleSourceEventArtifact,
    CaseAttemptArtifact,
)
from evaluation.compiler import compile_dataset, cross_session_source_sha256
from evaluation.config import EvalConfig
from evaluation.isolation import (
    allocate_bundle_qa_resources,
    allocate_bundle_resources,
    create_isolation_plan,
    isolation_plan_sha256,
    kira_benchmark_username,
    new_isolation_ledger,
    register_case_resources,
)
from evaluation.models import (
    BenchmarkVariant,
    CandidateChangeScope,
    CandidateDeclaration,
    GitSource,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.pc_acceptance import build_pc_acceptance
from evaluation.pc_preflight import PcPreflightFreeze, PcPreflightRunSet, PcProviderIdentity
from evaluation.scoring import output_sha256

_DATASET_HASH = "d" * 64
_CONTROL_SHA = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
_CANDIDATE_SHA = "1" * 40


def _config_hash(variant: BenchmarkVariant, suites: tuple[Suite, ...] = tuple(Suite)) -> str:
    target = "control" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "candidate"
    return EvalConfig(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=suites,
        gateway_url=f"http://{target}-gateway:8000",
        worker_url=f"http://{target}-worker:8001",
    ).fingerprint()


def _provenance(variant: BenchmarkVariant) -> RunProvenance:
    runtime_sha = _CONTROL_SHA if variant is BenchmarkVariant.HISTORICAL_CONTROL else _CANDIDATE_SHA
    package = (
        "2.0.20+viettel.3" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "2.0.20+viettel.6"
    )
    return RunProvenance(
        variant=variant,
        runtime=GitSource(sha=runtime_sha, dirty=False),
        harness=GitSource(sha="2" * 40, dirty=False),
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={"kira-context-memory": "0.4.1", "viettel-mem0": package},
        candidate=(
            CandidateDeclaration(
                candidate_id="candidate-a",
                change_scopes=(CandidateChangeScope.PROMPT,),
                summary="Evaluate the declared extraction prompt candidate.",
            )
            if variant is BenchmarkVariant.RELEASE_CANDIDATE
            else None
        ),
    )


def _small_compilation():
    full = compile_dataset(seed=742)
    selected = list(next(case for case in full.cases if case.suite is suite) for suite in Suite)
    return full.model_copy(update={"dataset_sha256": _DATASET_HASH, "cases": selected})


def _tiered_compilation():
    """One case per suite; cross-session is diagnostic_history, rest hard_gate."""

    compilation = _small_compilation()
    cases = []
    for case in compilation.cases:
        tier = "diagnostic_history" if case.suite is Suite.CROSS_SESSION else "hard_gate"
        cases.append(case.model_copy(update={"tags": (*case.tags, f"tier:{tier}")}))
    return compilation.model_copy(update={"cases": tuple(cases)})


def _write_store(
    root: Path,
    compilation,
    *,
    variant: BenchmarkVariant,
    safety: bool = False,
    case_outcome: Outcome | None = None,
    case_id: str | None = None,
    suites: tuple[Suite, ...] = tuple(Suite),
    selected_case_ids: tuple[str, ...] | None = None,
    config_sha256: str | None = None,
) -> None:
    selected_cases = tuple(
        case
        for case in compilation.cases
        if case.suite in suites and (selected_case_ids is None or case.case_id in selected_case_ids)
    )
    if selected_case_ids is not None:
        by_id = {case.case_id: case for case in selected_cases}
        selected_cases = tuple(by_id[case_id] for case_id in selected_case_ids)
    provenance = _provenance(variant)
    run_id = (
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        if variant is BenchmarkVariant.HISTORICAL_CONTROL
        else UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    )
    plan = create_isolation_plan(
        run_id=run_id,
        owner_token=UUID(int=700),
        conversation_database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
    )
    identity = ArtifactRunIdentity(
        run_id=run_id,
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        variant=variant,
        provenance=provenance,
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=_DATASET_HASH,
        compilation_sha256="e" * 64,
        config_sha256=config_sha256 or _config_hash(variant, suites),
        isolation_sha256=isolation_plan_sha256(plan),
        seed=742,
        suites=suites,
        selected_case_ids=tuple(case.case_id for case in selected_cases),
    )
    store = ArtifactStore.create(
        root, identity=identity, created_at=datetime(2026, 9, 18, tzinfo=UTC)
    )
    store.write_isolation_plan(plan)
    ledger = new_isolation_ledger(plan)
    source_cases = {}
    for case in selected_cases:
        if case.suite is Suite.CROSS_SESSION:
            source_cases.setdefault(case.case_id.split(":", 1)[0], case)
    for bundle_id, source_case in source_cases.items():
        source = allocate_bundle_resources(plan, bundle_id=bundle_id)
        ledger = register_case_resources(ledger, plan, source)
        inputs = source_case.inputs
        events = tuple(
            BundleSourceEventArtifact(
                event_id=UUID(int=index + 1),
                user_id=source.user_id,
                session_id=source.session_id,
                conversation_id=source.conversation_id,
                turn_id=f"source:{bundle_id}:{index}",
                boundary_message_id=2 * index + 2,
                completed=True,
            )
            for index in range(len(inputs.session_a_messages) // 2)
        )
        corpus = BundleSourceArtifact(
            identity=identity,
            bundle_id=bundle_id,
            logical_user_id=inputs.user_id,
            source_sha256=cross_session_source_sha256(inputs),
            persisted_user_id=source.user_id,
            source_session_id=source.session_id,
            source_conversation_id=source.conversation_id,
            expected_source_events=len(events),
            events=events,
            ready=True,
        )
        source = source.model_copy(update={"event_id": events[-1].event_id})
        ledger = register_case_resources(ledger, plan, source)
        store.write_bundle_source(corpus)
    for case in selected_cases:
        if case.suite is Suite.CROSS_SESSION:
            qa = allocate_bundle_qa_resources(
                plan, bundle_id=case.case_id.split(":", 1)[0], case_id=case.case_id, attempt=1
            )
            ledger = register_case_resources(ledger, plan, qa)
    store.write_isolation_ledger(ledger)
    for index, case in enumerate(selected_cases):
        targeted = case_id is not None and case.case_id == case_id
        execution_error = targeted and case_outcome in (
            Outcome.DEPENDENCY_ERROR,
            Outcome.PROTOCOL_ERROR,
            Outcome.NOT_RUN,
        )
        if execution_error:
            output = None
            output_hash = None
        else:
            output = {
                "diagnostic_metric": 0.1,
                "safety_violation_codes": (
                    ["cross_session_user_leak"] if safety and index == 0 else []
                ),
            }
            if case.suite is Suite.CROSS_SESSION:
                output.update(
                    {
                        arm: {
                            "kira_context_identity_sha256": sha256(
                                kira_benchmark_username(
                                    plan, case_id=case.case_id, attempt=1, arm=arm
                                ).encode()
                            ).hexdigest()
                        }
                        for arm in ("no_ltm", "with_ltm")
                    }
                )
            output_hash = output_sha256(output)
        store.append_case_attempt(
            CaseAttemptArtifact(
                case_id=case.case_id,
                suite=case.suite,
                attempt=1,
                completed_at=datetime(2026, 9, 18, tzinfo=UTC),
                outcome=(
                    case_outcome
                    if targeted
                    else Outcome.FAIL
                    if safety and index == 0
                    else Outcome.PASS
                ),
                output=output,
                output_sha256=output_hash,
                reason_codes=("provider_unavailable",) if execution_error else (),
            )
        )


def _evidence(tmp_path: Path, compilation) -> tuple[Path, Path]:
    image_manifest = tmp_path / "image-manifest.json"
    image_manifest.write_text(
        json.dumps(
            {
                "contract_id": "kira-week5-benchmark-v5",
                "variants": [
                    {
                        "variant_id": "control",
                        "runtime_revision": _CONTROL_SHA,
                        "metadata": {
                            "dataset_id": compilation.dataset_id,
                            "dataset_version": compilation.dataset_version,
                            "dataset_sha256": _DATASET_HASH,
                            "harness_revision": "2" * 40,
                            "prompt_sha256": _provenance(
                                BenchmarkVariant.HISTORICAL_CONTROL
                            ).prompt_sha256,
                            "package_versions": _provenance(
                                BenchmarkVariant.HISTORICAL_CONTROL
                            ).package_versions,
                        },
                    },
                    {
                        "variant_id": "candidate-a",
                        "runtime_revision": _CANDIDATE_SHA,
                        "metadata": {
                            "dataset_id": compilation.dataset_id,
                            "dataset_version": compilation.dataset_version,
                            "dataset_sha256": _DATASET_HASH,
                            "harness_revision": "2" * 40,
                            "prompt_sha256": _provenance(
                                BenchmarkVariant.RELEASE_CANDIDATE
                            ).prompt_sha256,
                            "package_versions": _provenance(
                                BenchmarkVariant.RELEASE_CANDIDATE
                            ).package_versions,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    preflight_path = tmp_path / "pc-preflight.json"
    variants = {}
    for name, variant in (
        ("control", BenchmarkVariant.HISTORICAL_CONTROL),
        ("candidate-a", BenchmarkVariant.RELEASE_CANDIDATE),
    ):
        variants[name] = PcPreflightFreeze(
            created_at=datetime(2026, 9, 18, tzinfo=UTC),
            dataset_id=compilation.dataset_id,
            dataset_version=compilation.dataset_version,
            dataset_sha256=_DATASET_HASH,
            config_sha256=_config_hash(variant),
            config_sha256_by_suites={
                ",".join(suite.value for suite in suites): _config_hash(variant, suites)
                for count in range(1, len(Suite) + 1)
                for suites in combinations(tuple(Suite), count)
                if Suite.RETRIEVAL not in suites or Suite.FORMATION in suites
            },
            provider_preflight_sha256="f" * 64,
            provider_run_id="00000000-0000-0000-0000-000000000001",
            kira_response_sha256="9" * 64,
            kira_identity_sha256="8" * 64,
            kira_event_count=1,
            kira_text_bytes=20,
            providers={"extraction_json": PcProviderIdentity(requested_model="model")},
            provenance=_provenance(variant),
        )
    preflight = PcPreflightRunSet(
        created_at=datetime(2026, 9, 18, tzinfo=UTC),
        dataset_sha256=_DATASET_HASH,
        variants=variants,
    )
    preflight_path.write_text(
        preflight.model_dump_json(exclude_computed_fields=True),
        encoding="utf-8",
    )
    return image_manifest, preflight_path


def _current_evidence(tmp_path: Path, compilation) -> tuple[Path, Path]:
    image_path, preflight_path = _evidence(tmp_path, compilation)
    image = json.loads(image_path.read_text(encoding="utf-8"))
    current_image = image["variants"][1]
    current_image["variant_id"] = "current"
    current_image["benchmark_variant"] = "current_runtime"
    image["variants"] = [current_image]
    image_path.write_text(json.dumps(image), encoding="utf-8")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    current = preflight["variants"]["candidate-a"]
    current["provenance"] = _provenance(BenchmarkVariant.CURRENT_RUNTIME).model_dump(
        mode="json", exclude_computed_fields=True
    )
    preflight["variants"] = {"current": current}
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")
    return image_path, preflight_path


def test_pc_acceptance_accepts_current_runtime_alone_without_quality_promotion(
    tmp_path, monkeypatch
):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(current, compilation, variant=BenchmarkVariant.CURRENT_RUNTIME)
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    result = build_pc_acceptance(
        run_roots={"current": current},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )
    assert result.technical_passed
    assert [item.variant_id for item in result.variants] == ["current"]
    assert result.quality_decision == "diagnostic_only_no_promotion"
    assert result.official is False
    assert result.selected_suites == tuple(Suite)
    assert result.evaluation_scope == "full_corpus"


def test_pc_acceptance_accepts_entire_qa_suite_from_full_preflight(tmp_path, monkeypatch):
    compilation = compile_dataset(seed=742).model_copy(update={"dataset_sha256": _DATASET_HASH})
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(
        current,
        compilation,
        variant=BenchmarkVariant.CURRENT_RUNTIME,
        suites=(Suite.CROSS_SESSION,),
    )
    image_manifest, preflight = _current_evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"current": current},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    assert result.technical_passed
    assert result.selected_suites == (Suite.CROSS_SESSION,)
    assert result.evaluation_scope == "selected_suites"
    assert result.variants[0].eligible_cases == 209
    assert result.variants[0].completed_eligible_cases == 209
    assert sum(result.variants[0].outcomes.values()) == 209
    assert result.quality_decision == "diagnostic_only_no_promotion"


def test_pc_acceptance_rejects_hidden_qa_subset(tmp_path, monkeypatch):
    compilation = compile_dataset(seed=742).model_copy(update={"dataset_sha256": _DATASET_HASH})
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    qa = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    current = tmp_path / "current"
    _write_store(
        current,
        compilation,
        variant=BenchmarkVariant.CURRENT_RUNTIME,
        suites=(Suite.CROSS_SESSION,),
        selected_case_ids=(qa.case_id,),
    )
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    with pytest.raises(ValueError, match="complete compiled corpus for its suites"):
        build_pc_acceptance(
            run_roots={"current": current},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


@pytest.mark.parametrize("suites", [(Suite.RETRIEVAL,), (Suite.CROSS_SESSION, Suite.REWRITE)])
def test_pc_acceptance_rejects_invalid_suite_selection(tmp_path, monkeypatch, suites):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(current, compilation, variant=BenchmarkVariant.CURRENT_RUNTIME, suites=suites)
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    with pytest.raises(ValueError, match="requires formation|canonical"):
        build_pc_acceptance(
            run_roots={"current": current},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


@pytest.mark.parametrize("difference", ["suites", "case_order"])
def test_pc_acceptance_variants_require_matching_suite_and_corpus_selection(
    tmp_path, monkeypatch, difference
):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(
        candidate,
        compilation,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        suites=(Suite.CROSS_SESSION,) if difference == "suites" else tuple(Suite),
        selected_case_ids=(
            tuple(case.case_id for case in reversed(compilation.cases))
            if difference == "case_order"
            else None
        ),
    )
    image_manifest, preflight = _evidence(tmp_path, compilation)
    with pytest.raises(ValueError, match="same selected suites|same ordered corpus"):
        build_pc_acceptance(
            run_roots={"control": control, "candidate-a": candidate},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


def test_pc_acceptance_qa_projection_keeps_non_suite_config_bound(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    drifted = EvalConfig(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=(Suite.CROSS_SESSION,),
        gateway_url="http://another-gateway:8000",
        worker_url="http://candidate-worker:8001",
    )
    _write_store(
        current,
        compilation,
        variant=BenchmarkVariant.CURRENT_RUNTIME,
        suites=(Suite.CROSS_SESSION,),
        config_sha256=drifted.fingerprint(),
    )
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    with pytest.raises(ValueError, match="config hash"):
        build_pc_acceptance(
            run_roots={"current": current},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


@pytest.mark.parametrize(
    ("outcome", "safety", "technical_passed"),
    [
        (Outcome.FAIL, False, True),
        (Outcome.PASS, True, False),
        (Outcome.DEPENDENCY_ERROR, False, False),
    ],
)
def test_qa_only_acceptance_preserves_quality_safety_and_dependency_rules(
    tmp_path, monkeypatch, outcome, safety, technical_passed
):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    qa = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    current = tmp_path / "current"
    _write_store(
        current,
        compilation,
        variant=BenchmarkVariant.CURRENT_RUNTIME,
        suites=(Suite.CROSS_SESSION,),
        case_outcome=outcome,
        case_id=qa.case_id,
        safety=safety,
    )
    image_manifest, preflight = _current_evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"current": current},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    assert result.technical_passed is technical_passed
    assert result.variants[0].eligible_cases == 1
    assert bool(result.variants[0].safety_violation_codes) is safety
    assert bool(result.variants[0].unresolved_case_ids) is (outcome is Outcome.DEPENDENCY_ERROR)


@pytest.mark.parametrize("mutation", ["hash", "short_source", "owner"])
@pytest.mark.parametrize("suites", [tuple(Suite), (Suite.CROSS_SESSION,)])
def test_pc_acceptance_rejects_unbound_or_incomplete_shared_source(
    tmp_path, monkeypatch, mutation, suites
):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(current, compilation, variant=BenchmarkVariant.CURRENT_RUNTIME, suites=suites)
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    path = next((current / "diagnostics" / "bundles").glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "hash":
        document["source_sha256"] = "0" * 64
    elif mutation == "short_source":
        document["events"] = document["events"][:-1]
        document["expected_source_events"] -= 1
    else:
        document["persisted_user_id"] = "another-user"
        for event in document["events"]:
            event["user_id"] = "another-user"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="trajectory|resource owner"):
        build_pc_acceptance(
            run_roots={"current": current},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


def test_pc_acceptance_missing_source_evidence_leaves_qa_incomplete(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(current, compilation, variant=BenchmarkVariant.CURRENT_RUNTIME)
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    next((current / "diagnostics" / "bundles").glob("*.json")).unlink()
    result = build_pc_acceptance(
        run_roots={"current": current},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )
    qa = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    assert not result.technical_passed
    assert result.variants[0].unresolved_case_ids == (qa.case_id,)


def test_pc_acceptance_rejects_reused_kira_context_between_qa_arms(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    current = tmp_path / "current"
    _write_store(current, compilation, variant=BenchmarkVariant.CURRENT_RUNTIME)
    image_manifest, preflight = _current_evidence(tmp_path, compilation)
    path = current / "cases.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    qa = next(record for record in records if record["suite"] == "cross_session")
    qa["output"]["with_ltm"]["kira_context_identity_sha256"] = qa["output"]["no_ltm"][
        "kira_context_identity_sha256"
    ]
    qa["output_sha256"] = output_sha256(qa["output"])
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(ValueError, match="isolated arm/attempt"):
        build_pc_acceptance(
            run_roots={"current": current},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


def test_pc_acceptance_passes_only_technical_gates_without_quality_decision(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(candidate, compilation, variant=BenchmarkVariant.RELEASE_CANDIDATE)
    image_manifest, preflight = _evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"control": control, "candidate-a": candidate},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    assert result.technical_passed
    assert result.quality_decision == "diagnostic_only_no_promotion"
    assert result.official is False
    assert [item.variant_id for item in result.variants] == ["control", "candidate-a"]
    assert result.schema_version == 2
    assert result.variants[0].config_sha256 != result.variants[1].config_sha256


def test_pc_acceptance_rejects_preflight_for_another_variant_target(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(candidate, compilation, variant=BenchmarkVariant.RELEASE_CANDIDATE)
    image_manifest, preflight = _evidence(tmp_path, compilation)
    document = json.loads(preflight.read_text(encoding="utf-8"))
    document["variants"]["control"]["config_sha256"] = document["variants"]["candidate-a"][
        "config_sha256"
    ]
    preflight.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="config hash"):
        build_pc_acceptance(
            run_roots={"control": control, "candidate-a": candidate},
            dataset_root=tmp_path,
            image_manifest_path=image_manifest,
            pc_preflight_path=preflight,
        )


def test_pc_acceptance_safety_failure_blocks_handoff_but_does_not_promote(tmp_path, monkeypatch):
    compilation = _small_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(candidate, compilation, variant=BenchmarkVariant.RELEASE_CANDIDATE, safety=True)
    image_manifest, preflight = _evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"control": control, "candidate-a": candidate},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    assert not result.technical_passed
    assert result.variants[1].safety_violation_codes == ("cross_session_user_leak",)
    assert result.quality_decision == "diagnostic_only_no_promotion"


def test_diagnostic_semantic_fail_does_not_fail_pc_technical_acceptance(tmp_path, monkeypatch):
    """Regression: a diagnostic_history semantic FAIL stays observable (in the
    outcome counter) but PC acceptance is a technical gate — a judged quality
    failure must not flip technical_passed or block the handoff."""

    compilation = _tiered_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    diagnostic_case = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(
        candidate,
        compilation,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        case_outcome=Outcome.FAIL,
        case_id=diagnostic_case.case_id,
    )
    image_manifest, preflight = _evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"control": control, "candidate-a": candidate},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    candidate_variant = result.variants[1]
    assert result.technical_passed
    assert candidate_variant.technical_passed
    assert candidate_variant.outcomes[Outcome.FAIL] == 1  # observable, non-promotional
    assert not candidate_variant.unresolved_case_ids
    assert not candidate_variant.safety_violation_codes


def test_diagnostic_safety_violation_still_fails_pc_technical_acceptance(tmp_path, monkeypatch):
    """Regression: tier exclusion must never reach PC acceptance — a
    diagnostic_history case with a safety violation blocks the handoff."""

    compilation = _tiered_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    diagnostic_case = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(candidate, compilation, variant=BenchmarkVariant.RELEASE_CANDIDATE)

    # Rewrite the diagnostic attempt's persisted output to carry a violation code;
    # _safety_codes recursively extracts codes regardless of the case's tier.
    attempts_path = candidate / "cases.jsonl"
    mutated = []
    for line in attempts_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["case_id"] == diagnostic_case.case_id:
            record["output"]["safety_violation_codes"] = ["cross_session_user_leak"]
            record["output_sha256"] = output_sha256(record["output"])
        mutated.append(json.dumps(record))
    attempts_path.write_text("\n".join(mutated) + "\n", encoding="utf-8")

    image_manifest, preflight = _evidence(tmp_path, compilation)
    result = build_pc_acceptance(
        run_roots={"control": control, "candidate-a": candidate},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    candidate_variant = result.variants[1]
    assert not result.technical_passed
    assert not candidate_variant.technical_passed
    assert candidate_variant.safety_violation_codes == ("cross_session_user_leak",)


def test_diagnostic_dependency_error_still_leaves_run_incomplete(tmp_path, monkeypatch):
    """Regression: a diagnostic_history case with DEPENDENCY_ERROR (no persisted
    output) must count as unresolved — infrastructure failures are never
    laundered out by the tier."""

    compilation = _tiered_compilation()
    monkeypatch.setattr(
        "evaluation.pc_acceptance.compile_dataset", lambda *_args, **_kwargs: compilation
    )
    diagnostic_case = next(case for case in compilation.cases if case.suite is Suite.CROSS_SESSION)
    control = tmp_path / "control"
    candidate = tmp_path / "candidate-a"
    _write_store(control, compilation, variant=BenchmarkVariant.HISTORICAL_CONTROL)
    _write_store(
        candidate,
        compilation,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        case_outcome=Outcome.DEPENDENCY_ERROR,
        case_id=diagnostic_case.case_id,
    )
    image_manifest, preflight = _evidence(tmp_path, compilation)

    result = build_pc_acceptance(
        run_roots={"control": control, "candidate-a": candidate},
        dataset_root=tmp_path,
        image_manifest_path=image_manifest,
        pc_preflight_path=preflight,
    )

    candidate_variant = result.variants[1]
    assert not result.technical_passed
    assert candidate_variant.unresolved_case_ids == (diagnostic_case.case_id,)
    assert candidate_variant.completed_eligible_cases < candidate_variant.eligible_cases
