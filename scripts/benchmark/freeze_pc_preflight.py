"""Create an immutable sanitized PC preflight freeze manifest."""

import argparse
import json
import sys
from pathlib import Path

from evaluation.dataset import default_dataset_root
from evaluation.pc_preflight import freeze_pc_preflight_run_set


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider-preflight",
        action="append",
        required=True,
        metavar="VARIANT=PATH",
        help="repeat for control and each declared candidate",
    )
    parser.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output exists")
        paths = {}
        for entry in args.provider_preflight:
            variant, separator, raw_path = entry.partition("=")
            if not separator or not variant or not raw_path or variant in paths:
                raise ValueError("each preflight needs a unique variant=path declaration")
            paths[variant] = Path(raw_path)
        frozen = freeze_pc_preflight_run_set(
            provider_preflight_paths=paths,
            dataset_root=args.dataset_root,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            frozen.model_dump_json(indent=2, exclude_computed_fields=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "official": False,
                    "variant_config_sha256": {
                        name: item.config_sha256 for name, item in frozen.variants.items()
                    },
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
