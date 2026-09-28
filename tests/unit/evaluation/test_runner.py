"""Company-PC profile policy and crash-safe full-corpus runner tests."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.config import load_config
from evaluation.dataset import default_dataset_root
from evaluation.models import BenchmarkVariant, GitSource, Profile, RunProvenance, Suite
from evaluation.runner import (
    MockBenchmarkExecutor,
    create_or_resume_store,
    execute_benchmark_cases,
    prepare_benchmark_run,
)

_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _provenance() -> RunProvenance:
    source = GitSource(sha="1" * 40, dirty=False)
    return RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.6",
        },
    )


def _approved_dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "benchmark_ready"
    manifest["data_policy"]["external_provider_allowed"] = True
    for bundle in manifest["bundles"]:
        bundle["contract_status"] = "frozen"
        bundle["materialization_status"] = "materialized"
        bundle["review"] = {"status": "reviewed", "reviewer": "mentor", "revision": "gold-v1"}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root


def test_pc_profile_rejects_unreviewed_or_unapproved_canonical_dataset(tmp_path):
    # The canonical dataset is frozen benchmark-ready; fabricate the unapproved
    # pre-review state (materialized + draft bundles, external approval withheld).
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "materialized"
    manifest["data_policy"]["external_provider_allowed"] = False
    for bundle in manifest["bundles"]:
        bundle["review"] = {"status": "draft", "reviewer": None, "revision": None}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    config = load_config(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=(Suite.REWRITE,),
        environment={},
    )
    with pytest.raises(ValueError, match="benchmark-ready"):
        prepare_benchmark_run(
            run_id=_RUN_ID,
            config=config,
            provenance=_provenance(),
            dataset_root=root,
            seed=742,
        )


def test_pc_profile_preparation_is_nonofficial_and_external_synthetic_stays_rejected(tmp_path):
    root = _approved_dataset(tmp_path)
    config = load_config(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=(Suite.REWRITE,),
        environment={},
    )
    preparation, _ = prepare_benchmark_run(
        run_id=_RUN_ID,
        config=config,
        provenance=_provenance(),
        dataset_root=root,
        seed=742,
    )
    store = create_or_resume_store(
        tmp_path / "run",
        preparation,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )
    assert store.manifest.environment_role == "pc_acceptance"
    assert store.manifest.official is False

    external = load_config(
        profile=Profile.EXTERNAL_SYNTHETIC,
        suites=(Suite.REWRITE,),
        environment={},
    )
    with pytest.raises(ValueError, match="cannot consume"):
        prepare_benchmark_run(
            run_id=_RUN_ID,
            config=external,
            provenance=_provenance(),
            dataset_root=root,
            seed=742,
        )


def test_preparation_groups_cases_in_native_dependency_order():
    config = load_config(profile=Profile.MOCK, suites=tuple(reversed(tuple(Suite))))
    preparation, _ = prepare_benchmark_run(
        run_id=_RUN_ID,
        config=config,
        provenance=_provenance(),
        dataset_root=default_dataset_root(),
        seed=742,
    )

    suite_order = tuple(dict.fromkeys(case.suite for case in preparation.selected_cases))
    assert suite_order == tuple(Suite)


async def test_mock_full_corpus_runner_persists_and_resumes_without_reexecution(tmp_path):
    config = load_config(profile=Profile.MOCK, suites=(Suite.REWRITE,))
    preparation, _ = prepare_benchmark_run(
        run_id=_RUN_ID,
        config=config,
        provenance=_provenance(),
        dataset_root=default_dataset_root(),
        seed=742,
    )
    store = create_or_resume_store(tmp_path / "run", preparation)

    await execute_benchmark_cases(preparation, store, MockBenchmarkExecutor())
    first_lines = (store.root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    await execute_benchmark_cases(preparation, store, MockBenchmarkExecutor())

    assert len(first_lines) == len(preparation.selected_cases)
    assert (store.root / "cases.jsonl").read_text(encoding="utf-8").splitlines() == first_lines
    assert not store.resume_plan().run_case_ids
