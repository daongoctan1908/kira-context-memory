"""D0 runner governance tests: canonical policy reuse and fail-closed real runs.

Real-run policy failures must surface before any provider transport is constructed
or called (embedding calls == 0, decision calls == 0). Dataset maturity is varied
with fabricated manifest fixtures copied from the canonical dataset; the canonical
dataset itself is never modified.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

from evaluation.d0_governance import evaluate_d0_policy, exposure_of
from evaluation.dataset import DatasetManifest, default_dataset_root, load_manifest
from evaluation.models import Profile

REPO_ROOT = default_dataset_root().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "benchmark"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import d0_dry_run as runner  # noqa: E402


def _fabricated_manifest(tmp_path: Path, *, ready: bool, external: bool) -> Path:
    """Copy the canonical dataset and rewrite ONLY manifest policy fields.

    ``ready=False`` fabricates a pre-review draft manifest: the canonical dataset
    is frozen benchmark-ready, so the draft state must be produced explicitly."""
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    path = root / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if ready:
        document["status"] = "benchmark_ready"
        for bundle in document["bundles"]:
            bundle["contract_status"] = "frozen"
            bundle["materialization_status"] = "materialized"
            bundle["review"] = {
                "status": "reviewed",
                "reviewer": "mentor",
                "revision": "gold-v1",
            }
    else:
        document["status"] = "materialized"
        for bundle in document["bundles"]:
            bundle["review"] = {
                "status": "draft",
                "reviewer": None,
                "revision": None,
            }
    document["data_policy"]["external_provider_allowed"] = ready and external
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root


def _manifest_at(root: Path) -> DatasetManifest:
    return load_manifest(root)


# ---------------------------------------------------------------------------
# Governance decision unit coverage
# ---------------------------------------------------------------------------


def test_profile_exposure_mapping_is_total_and_conservative():
    assert exposure_of(Profile.INTERNAL_TEST) == "internal"
    assert exposure_of(Profile.PC_OPENAI_ACCEPTANCE) == "external"
    # Mock and external_synthetic cannot declare any D0 exposure.
    assert exposure_of(Profile.MOCK) is None
    assert exposure_of(Profile.EXTERNAL_SYNTHETIC) is None


def test_exposure_profile_mismatch_fails_closed():
    manifest = _manifest_at(default_dataset_root())
    decision = evaluate_d0_policy(
        manifest,
        Profile.INTERNAL_TEST,
        declared_exposure="external",
        real_run=False,
    )
    assert not decision.eligible
    assert any("does not match" in reason for reason in decision.blocking_reasons)


def test_dry_run_on_draft_reports_blockers_without_raising(tmp_path):
    root = _fabricated_manifest(tmp_path, ready=False, external=False)
    manifest = _manifest_at(root)
    decision = evaluate_d0_policy(
        manifest,
        Profile.INTERNAL_TEST,
        declared_exposure="internal",
        real_run=False,
    )
    assert decision.governance_status == "blocked"
    assert not decision.eligible
    assert any("benchmark-ready" in reason for reason in decision.blocking_reasons)


# ---------------------------------------------------------------------------
# Runner-level fail-closed behavior: policy failure precedes any transport/call
# ---------------------------------------------------------------------------


class _CallCounters:
    """Fake ports proving policy failure precedes any provider interaction."""

    def __init__(self) -> None:
        self.embedding_calls = 0
        self.decision_calls = 0

    def as_ports(self, config):
        counters = self

        class _Embeddings:
            async def embed(self, texts):
                counters.embedding_calls += 1
                return [[0.0] for _ in texts]

            @staticmethod
            def cosine(left, right):
                return 0.0

        class _Decisions:
            async def decide(self, request_hash, request):
                counters.decision_calls += 1
                return {"decision": "KEEP_BOTH", "target_memory_id": None}

        return _Embeddings(), _Decisions()


@pytest.fixture()
def counters() -> _CallCounters:
    return _CallCounters()


def _run_real(tmp_path: Path, counters: _CallCounters, exposure: str) -> object:
    return runner.real_run(
        tmp_path / "unused.env",
        exposure,  # type: ignore[arg-type]
        ("S0",),
        ports=counters.as_ports,
    )


def test_real_run_draft_internal_blocked_before_any_call(tmp_path, counters):
    root = _fabricated_manifest(tmp_path, ready=False, external=False)
    # Point the runner at the fabricated dataset root for this test process.
    original_root = runner.DATASET_ROOT
    runner.DATASET_ROOT = root
    try:
        with pytest.raises(ValueError, match="benchmark-ready"):
            _run_real(tmp_path, counters, "internal")
    finally:
        runner.DATASET_ROOT = original_root
    assert counters.embedding_calls == 0
    assert counters.decision_calls == 0


def test_real_run_draft_external_blocked_before_any_call(tmp_path, counters):
    root = _fabricated_manifest(tmp_path, ready=False, external=False)
    original_root = runner.DATASET_ROOT
    runner.DATASET_ROOT = root
    try:
        with pytest.raises(ValueError, match="blocked by dataset policy"):
            _run_real(tmp_path, counters, "external")
    finally:
        runner.DATASET_ROOT = original_root
    assert counters.embedding_calls == 0
    assert counters.decision_calls == 0


def test_real_run_unapproved_external_dataset_blocked(tmp_path, counters):
    # benchmark_ready + reviewed but external_provider_allowed=false: internal would
    # pass, external must still be blocked by the canonical PC gate.
    root = _fabricated_manifest(tmp_path, ready=True, external=False)
    original_root = runner.DATASET_ROOT
    runner.DATASET_ROOT = root
    try:
        with pytest.raises(ValueError, match="not approved for the PC external provider"):
            _run_real(tmp_path, counters, "external")
    finally:
        runner.DATASET_ROOT = original_root
    assert counters.embedding_calls == 0
    assert counters.decision_calls == 0


def test_policy_passes_reviewed_internal_without_external_flag(tmp_path):
    # benchmark_ready + reviewed + external=false: INTERNAL_TEST passes policy.
    root = _fabricated_manifest(tmp_path, ready=True, external=False)
    manifest = _manifest_at(root)
    decision = evaluate_d0_policy(
        manifest,
        Profile.INTERNAL_TEST,
        declared_exposure="internal",
        real_run=True,
    )
    assert decision.eligible
    assert decision.blocking_reasons == ()


def test_policy_passes_reviewed_external_with_external_flag(tmp_path):
    root = _fabricated_manifest(tmp_path, ready=True, external=True)
    manifest = _manifest_at(root)
    decision = evaluate_d0_policy(
        manifest,
        Profile.PC_OPENAI_ACCEPTANCE,
        declared_exposure="external",
        real_run=True,
    )
    assert decision.eligible
    assert decision.blocking_reasons == ()
