"""Create an immutable sanitized PC preflight freeze manifest."""

import argparse
import json
import sys
from pathlib import Path

from evaluation.dataset import default_dataset_root
from evaluation.pc_preflight import freeze_pc_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-preflight", type=Path, required=True)
    parser.add_argument("--materialization-checkpoint", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output exists")
        frozen = freeze_pc_preflight(
            provider_preflight_path=args.provider_preflight,
            materialization_checkpoint_path=args.materialization_checkpoint,
            dataset_root=args.dataset_root,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(frozen.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "official": False,
                    "config_sha256": frozen.config_sha256,
                    "dataset_sha256": frozen.dataset_sha256,
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError):
        print(json.dumps({"outcome": "NOT_RUN", "reason": "pc_preflight_freeze_failed"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
