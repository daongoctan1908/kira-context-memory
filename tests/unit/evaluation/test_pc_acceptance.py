"""PC acceptance is a technical gate, never a candidate promotion scorer."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore, CaseAttemptArtifact
from evaluation.compiler import compile_dataset
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
from evaluation.pc_preflight import PcPreflightFreeze, PcProviderIdentity
from evaluation.scoring import output_sha256

_DATASET_HASH = "d" * 64
_CONFIG_HASH = "c" * 64
_CONTROL_SHA = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
_CANDIDATE_SHA = "1" * 40


def _provenance(variant: BenchmarkVariant) -> RunProvenance:
    runtime_sha = _CONTROL_SHA if variant is BenchmarkVariant.HISTORICAL_CONTROL else _CANDIDATE_SHA
    package = (
        "2.0.20+viettel.3" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "2.0.20+viettel.4"
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
    selected = tuple(next(case for case in full.cases if case.suite is suite) for suite in Suite)
    return full.model_copy(update={"dataset_sha256": _DATASET_HASH, "cases": selected})


def _write_store(
    root: Path,
    compilation,
    *,
    variant: BenchmarkVariant,
    safety: bool = False,
) -> None:
    provenance = _provenance(variant)
    identity = ArtifactRunIdentity(
        run_id=(
            UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            if variant is BenchmarkVariant.HISTORICAL_CONTROL
            else UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        ),
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        variant=variant,
        provenance=provenance,
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=_DATASET_HASH,
        compilation_sha256="e" * 64,
        config_sha256=_CONFIG_HASH,
        seed=742,
        suites=tuple(Suite),
        selected_case_ids=tuple(case.case_id for case in compilation.cases),
    )
    store = ArtifactStore.create(
        root, identity=identity, created_at=datetime(2026, 9, 18, tzinfo=UTC)
    )
    for index, case in enumerate(compilation.cases):
        output = {
            "diagnostic_metric": 0.1,
            "safety_violation_codes": (
                ["cross_session_user_leak"] if safety and index == 0 else []
            ),
        }
        store.append_case_attempt(
            CaseAttemptArtifact(
                case_id=case.case_id,
                suite=case.suite,
                attempt=1,
                completed_at=datetime(2026, 9, 18, tzinfo=UTC),
                outcome=Outcome.FAIL if safety and index == 0 else Outcome.PASS,
                output=output,
                output_sha256=output_sha256(output),
            )
        )


def _evidence(tmp_path: Path, compilation) -> tuple[Path, Path]:
    image_manifest = tmp_path / "image-manifest.json"
    image_manifest.write_text(
        json.dumps(
            {
                "contract_id": "kira-week5-benchmark-v4",
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
    preflight = PcPreflightFreeze(
        created_at=datetime(2026, 9, 18, tzinfo=UTC),
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=_DATASET_HASH,
        config_sha256=_CONFIG_HASH,
        provider_preflight_sha256="f" * 64,
        materialization_checkpoint_sha256="9" * 64,
        kira_completed_tasks=80,
        providers={
            "extraction_json": PcProviderIdentity(requested_model="model"),
        },
        provenance=_provenance(BenchmarkVariant.RELEASE_CANDIDATE),
    )
    preflight_path.write_text(
        preflight.model_dump_json(exclude_computed_fields=True),
        encoding="utf-8",
    )
    return image_manifest, preflight_path


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
