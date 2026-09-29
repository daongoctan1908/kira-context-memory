"""Holdout evaluation runner for the frozen d0_holdout_v1 adversarial slice.

Minimal wiring over the existing D0 decision port and the frozen holdout loader:
each of the 15 cases is sent to LLM#2 as the model-visible payload only
(existing_active_memories + candidate) under one registered prompt version, then
adjudicated offline against frozen gold. Runs fail closed on slice/hash/review
drift (load_holdout), invalid decisions, and unknown prompt versions.

Artifacts are exploratory characterization evidence (official: false), written to
artifacts/benchmark/d0-holdout/holdout-run-NNN/{manifest.json,predictions.jsonl,report.json}.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from evaluation.config import Profile, load_config  # noqa: E402
from evaluation.d0_holdout import (  # noqa: E402
    adjudicate_holdout,
    decide_holdout_case,
    load_holdout,
)
from evaluation.d0_local_executor import response_schema_fingerprint  # noqa: E402
from evaluation.d0_ports import D0DecisionPort, resolve_conflict_prompt  # noqa: E402

ARTIFACT_ROOT = REPOSITORY_ROOT / "artifacts" / "benchmark" / "d0-holdout"

_LABELS = ("DUPLICATE", "KEEP_BOTH", "SUPERSEDE")

_PROFILE_BY_EXPOSURE = {
    "internal": Profile.INTERNAL_TEST,
    "pc": Profile.PC_OPENAI_ACCEPTANCE,
}


def _artifact_dir() -> Path:
    existing = sorted(p for p in ARTIFACT_ROOT.glob("holdout-run-*") if p.is_dir())
    index = 1
    if existing:
        index = int(existing[-1].name.rsplit("-", 1)[-1]) + 1
    path = ARTIFACT_ROOT / f"holdout-run-{index:03d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _git_state() -> tuple[str, bool]:
    def git(*args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()

    dirty = git("status", "--porcelain=v1", "--untracked-files=normal")
    return git("rev-parse", "HEAD"), bool(dirty)


async def run_holdout(
    env_file: Path,
    exposure: str,
    *,
    prompt_version: str,
    decisions_port=None,
) -> Path:
    manifest, cases = load_holdout()  # fail-closed on any slice/hash/review drift
    if manifest.status != "approved_frozen":
        raise ValueError("holdout slice is not approved_frozen; refusing to run")
    if prompt_version not in manifest.provenance.prompt_versions_evaluated:
        raise ValueError(f"prompt version {prompt_version!r} not declared in holdout provenance")

    profile = _PROFILE_BY_EXPOSURE[exposure]
    config = load_config(
        profile=profile,
        suites=("formation",),
        env_file=env_file,
        formation_mode="write_free",
    )
    _, system_prompt = resolve_conflict_prompt(prompt_version)
    prompt_sha = sha256(system_prompt.encode("utf-8")).hexdigest()
    commit, dirty = _git_state()

    if decisions_port is None:
        import httpx

        decisions_port = D0DecisionPort(
            httpx.AsyncClient(follow_redirects=False), config, prompt_version=prompt_version
        )

    cache: dict = {}
    predictions = [
        await decide_holdout_case(
            decisions_port, str(config.extraction.model), prompt_version, case, cache
        )
        for case in cases
    ]
    adjudications = [
        adjudicate_holdout(prediction, case)
        for case, prediction in zip(cases, predictions, strict=True)
    ]

    out_dir = _artifact_dir()
    with (out_dir / "predictions.jsonl").open("w", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(prediction.model_dump_json() + "\n")

    by_label = {label: {"total": 0, "exact": 0} for label in _LABELS}
    for record in adjudications:
        bucket = by_label[record["gold_decision"]]
        bucket["total"] += 1
        if record["exact"]:
            bucket["exact"] += 1

    shared = {
        "run_kind": "holdout_real_run",
        "official": False,
        "git_commit": commit,
        "git_dirty": dirty,
        "declared_exposure": exposure,
        "profile": profile.value,
        "decision_model": str(config.extraction.model),
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha,
        "response_schema_sha256": response_schema_fingerprint(),
        "decoding_params": {"temperature": 0.0, "max_tokens": config.judge_max_tokens},
        "holdout_slice_id": manifest.slice_id,
        "holdout_slice_version": manifest.slice_version,
        "holdout_slice_sha256": manifest.slice_sha256,
        "holdout_case_count": manifest.case_count,
        "holdout_review_status": manifest.review.status,
        "holdout_review_revision": manifest.review.revision,
    }
    report = {
        **shared,
        "per_label": by_label,
        "overall_exact_total": sum(1 for r in adjudications if r["exact"]),
        "decision_mismatch_total": sum(1 for r in adjudications if r["decision_mismatch"]),
        "target_mismatch_total": sum(1 for r in adjudications if r["target_mismatch"]),
        "false_supersede_total": sum(1 for r in adjudications if r["false_supersede"]),
        "invalid_output_total": sum(1 for r in adjudications if r["invalid"]),
        "adjudications": adjudications,
    }
    _write(out_dir / "report.json", report)
    _write(
        out_dir / "manifest.json",
        {
            **shared,
            "embedding_provider": "not_used_holdout_no_retrieval",
            "holdout_frozen_prompt_pin": manifest.provenance.frozen_prompt_pin.model_dump(),
        },
    )
    print(
        json.dumps(
            {
                "overall_exact_total": report["overall_exact_total"],
                "per_label": report["per_label"],
                "false_supersede_total": report["false_supersede_total"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exposure", type=str, choices=("internal", "pc"), required=True)
    parser.add_argument("--prompt-version", type=str, required=True)
    parser.add_argument(
        "--env-file", type=Path, default=REPOSITORY_ROOT / ".env.benchmark.pc.local"
    )
    args = parser.parse_args()
    out_dir = asyncio.run(
        run_holdout(args.env_file, args.exposure, prompt_version=args.prompt_version)
    )
    print(f"artifacts: {out_dir}")


if __name__ == "__main__":
    main()
