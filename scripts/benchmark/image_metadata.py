"""Print secret-free benchmark metadata from inside an exact eval image."""

import json
import os
from hashlib import sha256
from importlib.metadata import version

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.rewrite_prompt import REWRITE_SYSTEM_PROMPT
from evaluation.compiler import compile_dataset
from evaluation.dataset import default_dataset_root, load_manifest
from evaluation.models import BENCHMARK_CONTRACT_ID


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def image_metadata(*, require_frozen: bool = True) -> dict[str, object]:
    dataset_root = default_dataset_root()
    manifest = load_manifest(dataset_root)
    if require_frozen and (
        manifest.status != "benchmark_ready" or not manifest.data_policy.external_provider_allowed
    ):
        raise ValueError("eval images require the reviewed PC-approved benchmark dataset")
    compilation = compile_dataset(dataset_root, seed=742)
    return {
        "contract_id": BENCHMARK_CONTRACT_ID,
        "runtime_revision": os.environ["BENCHMARK_RUNTIME_REVISION"],
        "harness_revision": os.environ["BENCHMARK_HARNESS_REVISION"],
        "dataset_id": compilation.dataset_id,
        "dataset_version": compilation.dataset_version,
        "dataset_sha256": compilation.dataset_sha256,
        "prompt_sha256": {
            "memory_extraction": _digest(MEMORY_EXTRACTION_INSTRUCTIONS),
            "rewrite_system": _digest(REWRITE_SYSTEM_PROMPT),
        },
        "package_versions": {
            "kira-context-memory": version("kira-context-memory"),
            "viettel-mem0": version("viettel-mem0"),
        },
    }


def main() -> None:
    print(json.dumps(image_metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
