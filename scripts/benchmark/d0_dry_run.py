"""D0-local characterization runner: dry-run manifest and real execution.

Dry-run (--dry-run): no network, no provider transport. Computes corpus stats,
prediction cardinality, and the number of unique LLM#2 request hashes by simulating
retrieval over hash embeddings (pool shapes, not scores), then writes the run
manifest with governance fields and blocking reasons. A dry-run artifact is never
official or accepted evidence.

Real run (--real-run --exposure internal|pc): reuses the canonical dataset policy
(evaluation.runner.validate_canonical_policy) and fails closed BEFORE any httpx
transport or provider port is constructed. Exposure is an explicit run-level caller
declaration applying to every provider in the run; the repo has no trusted hostname
classification, so exposure is never auto-detected.
"""

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from evaluation.config import Profile, load_config  # noqa: E402
from evaluation.d0_governance import D0Exposure, evaluate_d0_policy, exposure_of  # noqa: E402
from evaluation.d0_local_executor import (  # noqa: E402
    D0LocalExecutor,
    InvalidDecision,
    response_schema_fingerprint,
    s0_config,
    s1_config,
)
from evaluation.d0_metrics import (  # noqa: E402
    adjudicate,
    dataset_limitations,
    multi_target_reinforcement_diagnostic,
    safety_metrics,
    schedule_delta,
    structural_sensitivity,
    tier_metrics,
)
from evaluation.d0_ports import (  # noqa: E402
    D0_CONFLICT_PROMPT_VERSION,
    D0_CONFLICT_SYSTEM_PROMPT,
    D0DecisionPort,
    D0EmbeddingPort,
)
from evaluation.dataset import DatasetManifest, load_bundle, load_manifest  # noqa: E402
from evaluation.models import D0Decision, D0Schedule, Profile  # noqa: E402
from evaluation.shadow_lifecycle import ShadowTimeline  # noqa: E402

DATASET_ROOT = REPOSITORY_ROOT / "dataset" / "kira_ltm_v1"
ARTIFACT_ROOT = REPOSITORY_ROOT / "artifacts" / "benchmark" / "d0-local"

_EXPOSURE_CHOICES = ("internal", "external")
_PROFILE_BY_EXPOSURE: dict[str, Profile] = {
    "internal": Profile.INTERNAL_TEST,
    "external": Profile.PC_OPENAI_ACCEPTANCE,
}


class _ShapeEmbeddings:
    """Hash-based embeddings used ONLY to shape pools for dry-run counting.

    Score ordering here approximates real semantic similarity coarsely; the dry
    run never claims real scores. It exists to count request identities and
    cardinality without spending provider calls."""

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def _vector(self, text: str) -> list[float]:
        if text not in self._cache:
            digest = sha256(("d0-shape:" + text).encode("utf-8")).digest()
            raw = [b / 255.0 - 0.5 for b in digest[:64]]
            norm = sum(v * v for v in raw) ** 0.5
            self._cache[text] = [v / norm for v in raw]
        return self._cache[text]

    async def embed(self, texts):
        return [self._vector(text) for text in texts]


class _Counter:
    """Counts would-be LLM#2 calls and unique hashes; raises InvalidDecision so the
    executor caches it and never constructs a decision (dry-run semantic)."""

    def __init__(self) -> None:
        self.request_hashes: set[str] = set()
        self.calls = 0

    async def decide(self, request_hash: str, request):
        self.calls += 1
        self.request_hashes.add(request_hash)
        raise InvalidDecision("dry_run")


def _git_state() -> tuple[str, bool]:
    def git(*args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()

    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain=v1", "--untracked-files=normal"))


def _load_manifest() -> DatasetManifest:
    return load_manifest(DATASET_ROOT)


def _load_timeline() -> tuple[ShadowTimeline, tuple]:
    manifest = _load_manifest()
    bundles = tuple(load_bundle(entry, root=DATASET_ROOT) for entry in manifest.bundles)
    return ShadowTimeline(bundles), bundles


def _artifact_dir(kind: str) -> Path:
    existing = sorted(
        (p for p in ARTIFACT_ROOT.glob(f"{kind}-*") if p.is_dir()),
        key=lambda p: p.name,
    )
    index = 1
    if existing:
        index = int(existing[-1].name.rsplit("-", 1)[-1]) + 1
    path = ARTIFACT_ROOT / f"{kind}-{index:03d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _configs(config_ids: tuple[str, ...]):
    return tuple(s0_config() if cid == "S0" else s1_config() for cid in config_ids)


def _governance_fields(manifest_doc: DatasetManifest, decision) -> dict[str, object]:
    review_statuses = sorted({str(entry.review.status) for entry in manifest_doc.bundles})
    return {
        "governance_status": decision.governance_status,
        "real_run_eligible": decision.eligible,
        "blocking_reasons": list(decision.blocking_reasons),
        "dataset_status": str(manifest_doc.status),
        "dataset_review_status": "/".join(review_statuses),
        "data_policy_external_provider_allowed": manifest_doc.data_policy.external_provider_allowed,
    }


def dry_run(exposure: D0Exposure, config_ids: tuple[str, ...]) -> Path:
    manifest_doc = _load_manifest()
    profile = _PROFILE_BY_EXPOSURE[exposure]
    decision = evaluate_d0_policy(
        manifest_doc, profile, declared_exposure=exposure, real_run=False
    )
    timeline, _bundles = _load_timeline()
    commit, dirty = _git_state()
    counter = _Counter()
    executor = D0LocalExecutor(timeline, _ShapeEmbeddings(), counter)
    with contextlib.suppress(InvalidDecision):
        asyncio.run(executor.evaluate_all(_configs(config_ids)))
    stats = timeline.corpus_stats()
    manifest = {
        "run_kind": "dry_run",
        "official": False,
        "git_commit": commit,
        "git_dirty": dirty,
        "declared_exposure": exposure,
        "profile": profile.value,
        "dataset_id": "kira_ltm_v1",
        "dataset_version": str(manifest_doc.dataset_version),
        **_governance_fields(manifest_doc, decision),
        "embedding_provider": "NOT_WIRED_IN_DRY_RUN",
        "embedding_model": "NOT_WIRED_IN_DRY_RUN",
        "embedding_dimensions": 0,
        "decision_provider": "NOT_WIRED_IN_DRY_RUN",
        "decision_model": "NOT_WIRED_IN_DRY_RUN",
        "prompt_version": D0_CONFLICT_PROMPT_VERSION,
        "prompt_sha256": sha256(D0_CONFLICT_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "response_schema_sha256": response_schema_fingerprint(),
        "decoding_params": {"temperature": 0.0, "max_tokens": 512},
        "schedules": ["early", "late"],
        "retrieval_configs": list(config_ids),
        "top_k": 10,
        "corpus_stats": stats.model_dump(),
        "expected_oo_points": stats.conflict_evaluable_events * len(config_ids) * 2,
        "expected_unique_llm_requests_shape_approx": len(counter.request_hashes),
        "expected_llm_calls_without_cache_shape_approx": counter.calls,
        "shape_embedding_caveat": "pool shapes approximated by hash embeddings; real "
        "run replaces these counts with observed values",
        "known_limitations": [
            "dry-run artifact is exploratory characterization evidence, never an "
            "accepted or official benchmark result",
            "oracle candidates and oracle shadow bank (O/O), not native extraction",
            "CONVERSATION scope only; no GLOBAL or cross-scope cases in dataset",
            "full-corpus evaluation; no unseen holdout",
            "gold review status is draft, not reviewed",
            "formation boundary ambiguity represented by EARLY/LATE sensitivity bounds",
            "hard-negative slices (GLOBAL, cross-scope, KPI-period, exact-hash) missing",
            "semantic-only offline retrieval; production lexical search is PostgreSQL "
            "FTS ts_rank_cd and is not reproduced",
        ],
    }
    out_dir = _artifact_dir("dry-run")
    _write(out_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return out_dir


def real_run(
    env_file: Path,
    exposure: D0Exposure,
    config_ids: tuple[str, ...],
    *,
    ports=None,
) -> Path:
    """Execute the real characterization. ``ports`` is an injection seam for tests:
    (embeddings, decisions) factories taking the EvalConfig; production callers
    never pass it. Policy is enforced before any transport is constructed."""
    manifest_doc = _load_manifest()
    profile = _PROFILE_BY_EXPOSURE[exposure]
    decision = evaluate_d0_policy(
        manifest_doc, profile, declared_exposure=exposure, real_run=True
    )
    if not decision.eligible:
        reasons = "; ".join(decision.blocking_reasons)
        raise ValueError(f"D0 real run blocked by dataset policy: {reasons}")

    config = load_config(
        profile=profile,
        suites=("formation",),
        env_file=env_file,
        formation_mode="write_free",
    )
    timeline, _bundles = _load_timeline()
    commit, dirty = _git_state()
    if ports is None:
        import httpx

        embeddings = D0EmbeddingPort(httpx.AsyncClient(follow_redirects=False), config)
        decisions = D0DecisionPort(httpx.AsyncClient(follow_redirects=False), config)
    else:
        embeddings, decisions = ports(config)
    executor = D0LocalExecutor(timeline, embeddings, decisions, top_k=10)
    predictions = asyncio.run(executor.evaluate_all(_configs(config_ids)))

    out_dir = _artifact_dir("real-run")
    predictions_path = out_dir / "predictions.jsonl"
    with predictions_path.open("w", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(prediction.model_dump_json() + "\n")

    structural = structural_sensitivity(timeline)
    early = [p for p in predictions if p.schedule is D0Schedule.EARLY]
    late = [p for p in predictions if p.schedule is D0Schedule.LATE]
    delta = schedule_delta(early, late, structural=structural)
    outcomes = [adjudicate(p) for p in predictions]
    report: dict[str, object] = {
        "run_kind": "real_run",
        "official": False,
        "git_commit": commit,
        "git_dirty": dirty,
        "declared_exposure": exposure,
        "profile": profile.value,
        "embedding_model": str(config.embedding.model),
        "embedding_dimensions": config.embedding_dimensions,
        "decision_model": str(config.extraction.model),
        "decision_base_url": str(config.extraction.base_url),
        "retrieval_configs": [c.config_id for c in _configs(config_ids)],
        "prediction_count": len(predictions),
        "schedule_delta": {
            "robust": list(delta.robust_events),
            "boundary_moved": list(delta.boundary_moved_events),
            "bank_sensitive": list(delta.bank_sensitive_events),
            "scored_order_sensitive": list(delta.scored_order_sensitive_events),
            "decision_input_sensitive": list(delta.decision_input_sensitive_events),
            "outcome_sensitive": list(delta.outcome_sensitive_events),
        },
        "dataset_limitations": dataset_limitations(
            predictions,
            "/".join(sorted({str(entry.review.status) for entry in manifest_doc.bundles})),
        ),
    }
    for name, preds in (("EARLY", early), ("LATE", late)):
        sched_outcomes = [adjudicate(p) for p in preds]
        for cid in {c.config_id for c in _configs(config_ids)}:
            subset = [p for p in preds if p.config_id == cid]
            sub_outcomes = [adjudicate(p) for p in subset]
            sup = tier_metrics(sub_outcomes, "SUPERSEDE")
            dup = tier_metrics(sub_outcomes, "DUPLICATE")
            kb = tier_metrics(sub_outcomes, "KEEP_BOTH")
            safety = safety_metrics(sub_outcomes)
            report[f"{name}_{cid}"] = {
                "supersede": asdict(sup),
                "duplicate": asdict(dup),
                "keep_both": asdict(kb),
                "safety": asdict(safety),
            }
        sched_safety = safety_metrics(sched_outcomes)
        report[f"{name}_pooled_safety"] = asdict(sched_safety)
        report[f"{name}_multi_target_reinforcement_diagnostic"] = (
            multi_target_reinforcement_diagnostic(sched_outcomes)
        )
    # Case dumps for manual review, by corrected semantics.
    false_cases = [o.event_id for o in outcomes if o.model_false_supersede]
    decision_mismatch_cases = [o.event_id for o in outcomes if o.decision_mismatch]
    target_mismatch_cases = [o.event_id for o in outcomes if o.target_mismatch]
    pipeline_false_cases = [
        o.event_id
        for o in outcomes
        if o.llm_executed
        and o.decision is not None
        and o.decision.decision is D0Decision.SUPERSEDE
        and (
            o.gold_operation != "update"
            or not o.gold_target_ids
            or o.decision.target_memory_id != o.gold_target_ids[0]
        )
    ]
    report["model_false_supersede_case_ids"] = sorted(set(false_cases))
    report["decision_mismatch_case_ids"] = sorted(set(decision_mismatch_cases))
    report["target_mismatch_case_ids"] = sorted(set(target_mismatch_cases))
    report["pipeline_false_supersede_case_ids"] = sorted(set(pipeline_false_cases))
    _write(out_dir / "report.json", report)

    manifest = {
        "run_kind": "real_run",
        "official": False,
        "git_commit": commit,
        "git_dirty": dirty,
        "declared_exposure": exposure,
        "profile": profile.value,
        "dataset_id": "kira_ltm_v1",
        "dataset_version": str(manifest_doc.dataset_version),
        **_governance_fields(manifest_doc, decision),
        "embedding_provider": "openai_compatible",
        "embedding_model": str(config.embedding.model),
        "embedding_dimensions": config.embedding_dimensions,
        "decision_provider": "openai_compatible_extraction_endpoint",
        "decision_model": str(config.extraction.model),
        "prompt_version": D0_CONFLICT_PROMPT_VERSION,
        "prompt_sha256": sha256(D0_CONFLICT_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "response_schema_sha256": response_schema_fingerprint(),
        "decoding_params": {"temperature": 0.0, "max_tokens": config.judge_max_tokens},
        "schedules": ["early", "late"],
        "retrieval_configs": list(config_ids),
        "top_k": 10,
        "corpus_stats": timeline.corpus_stats().model_dump(),
        "observed_predictions": len(predictions),
        "known_limitations": report["dataset_limitations"],
    }
    _write(out_dir / "manifest.json", manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str)[:4000])
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="No network, no transports")
    parser.add_argument("--real-run", action="store_true")
    parser.add_argument(
        "--exposure",
        type=str,
        choices=_EXPOSURE_CHOICES,
        required=True,
        help="Run-level provider exposure declared for EVERY provider in this run; "
        "must match the selected profile mapping. Never auto-detected.",
    )
    parser.add_argument("--env-file", type=Path, default=REPOSITORY_ROOT / ".env.benchmark.pc.local")
    parser.add_argument(
        "--configs",
        type=str,
        default="S0,S1",
        help="Comma-separated subset of S0,S1,S_SWEEP",
    )
    args = parser.parse_args()
    exposure: D0Exposure = args.exposure  # type: ignore[assignment]
    config_ids = tuple(cid.strip().upper() for cid in args.configs.split(",") if cid.strip())
    if args.dry_run:
        dry_run(exposure, config_ids)
    elif args.real_run:
        real_run(args.env_file, exposure, config_ids)
    else:
        parser.error("choose --dry-run or --real-run")


if __name__ == "__main__":
    main()
