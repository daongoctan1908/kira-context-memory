"""Run the deterministic Week 5 handoff acceptance without network access."""

import argparse
import asyncio
import json
import os
import re
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.rewrite_prompt import REWRITE_SYSTEM_PROMPT
from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.config import load_config
from evaluation.dataset import default_dataset_root
from evaluation.models import (
    BenchmarkVariant,
    GitSource,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.preflight import run_preflight
from evaluation.provenance import capture_local_provenance
from scripts.validate_dataset import validate_dataset

_GIT_SHA = re.compile(r"^[a-f0-9]{40,64}$")


@dataclass(frozen=True, slots=True)
class MockAcceptanceResult:
    output_root: Path
    case_count: int
    dataset_sha256: str
    suite_outcomes: dict[str, str]


def _source_version(path: Path) -> str:
    return str(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"])


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _image_or_checkout_provenance() -> RunProvenance:
    revision = os.environ.get("BENCHMARK_SOURCE_REVISION", "").strip().lower()
    if not _GIT_SHA.fullmatch(revision):
        return capture_local_provenance()
    source = GitSource(sha=revision, dirty=False)
    root = Path(__file__).resolve().parents[1]
    return RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={
            "memory_extraction": _sha(MEMORY_EXTRACTION_INSTRUCTIONS),
            "rewrite_system": _sha(REWRITE_SYSTEM_PROMPT),
        },
        package_versions={
            "kira-context-memory": _source_version(root / "pyproject.toml"),
            "viettel-mem0": _source_version(root / "packages/viettel-mem0/pyproject.toml"),
        },
    )


def _write_new(path: Path, contents: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "xb" if isinstance(contents, bytes) else "x"
    kwargs = {} if isinstance(contents, bytes) else {"encoding": "utf-8", "newline": "\n"}
    with path.open(mode, **kwargs) as stream:
        stream.write(contents)


def _prepare_output_root(output_root: Path) -> Path:
    resolved = output_root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


async def run_mock_acceptance(
    output_root: Path,
    *,
    dataset_root: Path | None = None,
    seed: int = 742,
) -> MockAcceptanceResult:
    """Validate, compile and preflight every suite using only deterministic doubles."""

    root = dataset_root or default_dataset_root()
    validation = validate_dataset(root)
    if not validation.valid:
        raise ValueError("canonical dataset validation failed")
    compilation = compile_dataset(root, seed=seed)
    suites = tuple(Suite)
    config = load_config(profile=Profile.MOCK, suites=suites)
    preflight = await run_preflight(config, provenance=_image_or_checkout_provenance())
    if any(suite.outcome is not Outcome.PASS for suite in preflight.suites):
        raise ValueError("mock dependency preflight failed")

    resolved = _prepare_output_root(output_root)
    _write_new(resolved / "dataset-validation.json", validation.as_json() + "\n")
    _write_new(resolved / "compiled-cases.json", compilation_json_bytes(compilation))
    _write_new(resolved / "mock-preflight.json", preflight.model_dump_json(indent=2) + "\n")
    suite_outcomes = {suite.suite.value: suite.outcome.value for suite in preflight.suites}
    summary = {
        "schema_version": 1,
        "contract_id": "kira-week5-benchmark-v4",
        "profile": "mock",
        "network_required": False,
        "quality_claim": False,
        "dataset_sha256": compilation.dataset_sha256,
        "case_count": len(compilation.cases),
        "suite_outcomes": suite_outcomes,
        "seed": seed,
        "materialization_checkpoint": "artifacts/week5/kira-materialization.json",
        "benchmark_artifact_root": "artifacts/week5/benchmark",
    }
    _write_new(
        resolved / "mock-acceptance.json",
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return MockAcceptanceResult(
        output_root=resolved,
        case_count=len(compilation.cases),
        dataset_sha256=compilation.dataset_sha256,
        suite_outcomes=suite_outcomes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/week5/mock-acceptance"))
    parser.add_argument("--root", type=Path, default=default_dataset_root())
    parser.add_argument("--seed", type=int, default=742)
    args = parser.parse_args(argv)
    if args.seed < 0:
        parser.error("--seed must be nonnegative")
    try:
        result = asyncio.run(
            run_mock_acceptance(args.output, dataset_root=args.root, seed=args.seed)
        )
    except (OSError, ValueError):
        print(json.dumps({"outcome": "NOT_RUN", "reason": "mock_acceptance_error"}))
        return 2
    print(
        json.dumps(
            {
                "outcome": "PASS",
                "profile": "mock",
                "quality_claim": False,
                "case_count": result.case_count,
                "dataset_sha256": result.dataset_sha256,
                "output": str(result.output_root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
