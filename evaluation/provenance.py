"""Fail-closed local provenance capture for benchmark artifacts."""

import subprocess
import tomllib
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.rewrite_prompt import REWRITE_SYSTEM_PROMPT
from evaluation.models import BenchmarkVariant, GitSource, RunProvenance

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_version(distribution: str, pyproject: Path) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(document["project"]["version"])


def _text_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def capture_local_provenance() -> RunProvenance:
    """Describe this checkout without treating its runtime and harness as one concept."""
    current_sha = _git_output("rev-parse", "HEAD")
    dirty = bool(_git_output("status", "--porcelain=v1", "--untracked-files=normal"))
    source = GitSource(sha=current_sha, dirty=dirty)
    return RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={
            "memory_extraction": _text_sha256(MEMORY_EXTRACTION_INSTRUCTIONS),
            "rewrite_system": _text_sha256(REWRITE_SYSTEM_PROMPT),
        },
        package_versions={
            "kira-context-memory": _source_version(
                "kira-context-memory", _REPOSITORY_ROOT / "pyproject.toml"
            ),
            "viettel-mem0": _source_version(
                "viettel-mem0", _REPOSITORY_ROOT / "packages/viettel-mem0/pyproject.toml"
            ),
        },
    )
