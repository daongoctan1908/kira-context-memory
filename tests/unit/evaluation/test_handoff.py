"""Offline handoff evidence is immutable and contains no promotion decision."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.compiler import compile_dataset
from evaluation.dataset import DatasetDataPolicy, GoldReview, load_manifest
from evaluation.handoff import build_handoff_evidence, file_sha256
from evaluation.models import (
    BenchmarkVariant,
    CandidateChangeScope,
    CandidateDeclaration,
    GitSource,
    Outcome,
    RunProvenance,
)
from evaluation.pc_acceptance import PcAcceptanceManifest, PcVariantAcceptance
from evaluation.pc_preflight import PcPreflightFreeze, PcPreflightRunSet, PcProviderIdentity

_DATASET_HASH = "d" * 64
_CONFIG_HASH = "c" * 64
_CONTROL_SHA = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
_CANDIDATE_SHA = "1" * 40


def _provenance(variant: BenchmarkVariant) -> RunProvenance:
    return RunProvenance(
        variant=variant,
        runtime=GitSource(
            sha=_CONTROL_SHA if variant is BenchmarkVariant.HISTORICAL_CONTROL else _CANDIDATE_SHA,
            dirty=False,
        ),
        harness=GitSource(sha="2" * 40, dirty=False),
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": (
                "2.0.20+viettel.3"
                if variant is BenchmarkVariant.HISTORICAL_CONTROL
                else "2.0.20+viettel.6"
            ),
        },
        candidate=(
            CandidateDeclaration(
                candidate_id="candidate-a",
                change_scopes=(CandidateChangeScope.PROMPT,),
                summary="Declared candidate.",
            )
            if variant is BenchmarkVariant.RELEASE_CANDIDATE
            else None
        ),
    )


def _write_evidence(tmp_path: Path, compilation) -> tuple[Path, Path, Path]:
    image = tmp_path / "image-manifest.json"
    image.write_text(
        json.dumps(
            {
                "contract_id": "kira-week5-benchmark-v4",
                "variants": [
                    {"variant_id": "control"},
                    {"variant_id": "candidate-a"},
                ],
                "images": [
                    {"role": "control-runtime"},
                    {"role": "control-eval"},
                    {"role": "candidate-a-runtime"},
                    {"role": "candidate-a-eval"},
                    {"role": "postgres-dependency"},
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
            config_sha256=_CONFIG_HASH,
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
        preflight.model_dump_json(exclude_computed_fields=True), encoding="utf-8"
    )
    acceptance_path = tmp_path / "pc-acceptance.json"
    acceptance = PcAcceptanceManifest(
        created_at=datetime(2026, 9, 18, tzinfo=UTC),
        dataset_sha256=_DATASET_HASH,
        image_manifest_sha256=file_sha256(image),
        pc_preflight_sha256=file_sha256(preflight_path),
        variants=(
            PcVariantAcceptance(
                variant_id="control",
                benchmark_variant=BenchmarkVariant.HISTORICAL_CONTROL,
                runtime_revision=_CONTROL_SHA,
                config_sha256=_CONFIG_HASH,
                run_id="control-run",
                eligible_cases=1,
                completed_eligible_cases=1,
                outcomes={Outcome.PASS: 1},
                technical_passed=True,
            ),
            PcVariantAcceptance(
                variant_id="candidate-a",
                benchmark_variant=BenchmarkVariant.RELEASE_CANDIDATE,
                runtime_revision=_CANDIDATE_SHA,
                config_sha256=_CONFIG_HASH,
                run_id="candidate-run",
                eligible_cases=1,
                completed_eligible_cases=1,
                outcomes={Outcome.FAIL: 1},
                technical_passed=True,
            ),
        ),
        technical_passed=True,
    )
    acceptance_path.write_text(acceptance.model_dump_json(), encoding="utf-8")
    return image, preflight_path, acceptance_path


def test_handoff_freeze_binds_review_acceptance_and_dynamic_image_set(tmp_path, monkeypatch):
    source = load_manifest()
    reviewed = source.model_copy(
        update={
            "status": "benchmark_ready",
            "data_policy": DatasetDataPolicy(
                classification="internal_test_synthetic_derived",
                external_provider_allowed=True,
                public_git_status="review_required",
            ),
            "bundles": tuple(
                bundle.model_copy(
                    update={
                        "materialization_status": "materialized",
                        "review": GoldReview(
                            status="reviewed", reviewer="reviewer-1", revision="review-r1"
                        ),
                    }
                )
                for bundle in source.bundles
            ),
        }
    )
    compilation = compile_dataset().model_copy(update={"dataset_sha256": _DATASET_HASH})
    monkeypatch.setattr("evaluation.handoff.load_manifest", lambda _root: reviewed)
    monkeypatch.setattr("evaluation.handoff.compile_dataset", lambda *_args, **_kwargs: compilation)
    dataset_root = tmp_path / "dataset"
    dataset_root.mkdir()
    (dataset_root / "manifest.json").write_text("{}", encoding="utf-8")
    image, preflight, acceptance = _write_evidence(tmp_path, compilation)

    result = build_handoff_evidence(
        dataset_root=dataset_root,
        image_manifest_path=image,
        pc_preflight_path=preflight,
        pc_acceptance_path=acceptance,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    assert result.official is False
    assert result.variant_ids == ("control", "candidate-a")
    assert result.image_count == 5
    assert result.review_reviewer == "reviewer-1"


def test_handoff_freeze_rejects_image_manifest_changed_after_acceptance(tmp_path, monkeypatch):
    source = load_manifest()
    reviewed = source.model_copy(
        update={
            "status": "benchmark_ready",
            "data_policy": source.data_policy.model_copy(
                update={"external_provider_allowed": True}
            ),
            "bundles": tuple(
                bundle.model_copy(
                    update={
                        "materialization_status": "materialized",
                        "review": GoldReview(
                            status="reviewed", reviewer="reviewer-1", revision="review-r1"
                        ),
                    }
                )
                for bundle in source.bundles
            ),
        }
    )
    compilation = compile_dataset().model_copy(update={"dataset_sha256": _DATASET_HASH})
    monkeypatch.setattr("evaluation.handoff.load_manifest", lambda _root: reviewed)
    monkeypatch.setattr("evaluation.handoff.compile_dataset", lambda *_args, **_kwargs: compilation)
    dataset_root = tmp_path / "dataset"
    dataset_root.mkdir()
    (dataset_root / "manifest.json").write_text("{}", encoding="utf-8")
    image, preflight, acceptance = _write_evidence(tmp_path, compilation)
    image.write_text(image.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after PC acceptance"):
        build_handoff_evidence(
            dataset_root=dataset_root,
            image_manifest_path=image,
            pc_preflight_path=preflight,
            pc_acceptance_path=acceptance,
        )
