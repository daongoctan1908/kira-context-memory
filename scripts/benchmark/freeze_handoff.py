"""Freeze secret-free, hash-bound evidence before exporting the offline image archive."""

import argparse
import json
from pathlib import Path

from evaluation.dataset import default_dataset_root
from evaluation.handoff import build_handoff_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--pc-preflight", type=Path, required=True)
    parser.add_argument("--pc-acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output exists")
        result = build_handoff_evidence(
            dataset_root=args.dataset_root,
            image_manifest_path=args.image_manifest,
            pc_preflight_path=args.pc_preflight,
            pc_acceptance_path=args.pc_acceptance,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "official": False,
                    "variant_count": len(result.variant_ids),
                    "image_count": result.image_count,
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError):
        print(json.dumps({"outcome": "NOT_RUN", "reason": "handoff_evidence_invalid"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
