"""Check technical PC acceptance gates without making a quality promotion decision."""

import argparse
import json
from pathlib import Path

from evaluation.dataset import default_dataset_root
from evaluation.pc_acceptance import build_pc_acceptance


def _run_mapping(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        variant, separator, raw_path = value.partition("=")
        if not separator or not variant or not raw_path or variant in result:
            raise ValueError("--run must be unique VARIANT=PATH")
        result[variant] = Path(raw_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--pc-preflight", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output exists")
        result = build_pc_acceptance(
            run_roots=_run_mapping(args.run),
            dataset_root=args.dataset_root,
            image_manifest_path=args.image_manifest,
            pc_preflight_path=args.pc_preflight,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "outcome": "PASS" if result.technical_passed else "FAIL",
                    "official": False,
                    "quality_decision": result.quality_decision,
                    "selected_suites": [suite.value for suite in result.selected_suites],
                    "evaluation_scope": result.evaluation_scope,
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0 if result.technical_passed else 1
    except (OSError, ValueError):
        print(json.dumps({"outcome": "NOT_RUN", "reason": "pc_acceptance_check_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
