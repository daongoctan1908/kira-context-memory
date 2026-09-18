"""Export a hash-bound review packet and freeze an approved materialized dataset."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from evaluation.dataset import default_dataset_root
from evaluation.review import (
    BundleReviewDecision,
    DatasetReviewPacket,
    build_review_packet,
    decision_template,
    freeze_reviewed_dataset,
)

_DECISIONS = TypeAdapter(tuple[BundleReviewDecision, ...])


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="create the immutable review packet and template")
    export.add_argument("--root", type=Path, default=default_dataset_root())
    export.add_argument("--packet", type=Path, required=True)
    export.add_argument("--decisions", type=Path, required=True)

    freeze = commands.add_parser("freeze", help="apply complete human approvals atomically")
    freeze.add_argument("--root", type=Path, default=default_dataset_root())
    freeze.add_argument("--packet", type=Path, required=True)
    freeze.add_argument("--decisions", type=Path, required=True)
    freeze.add_argument("--dataset-version", required=True)
    freeze.add_argument("--allow-pc-openai", action="store_true")
    destination = freeze.add_mutually_exclusive_group(required=True)
    destination.add_argument("--in-place", action="store_true")
    destination.add_argument("--output-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            if args.packet.exists() or args.decisions.exists():
                raise FileExistsError("review output already exists")
            packet = build_review_packet(args.root)
            _write_new(args.packet, packet.model_dump(mode="json"))
            _write_new(args.decisions, decision_template(packet))
            print(
                f"PASS bundles={len(packet.bundles)} source_sha256={packet.source_sha256} "
                f"packet={args.packet} decisions={args.decisions}"
            )
            return 0

        packet = DatasetReviewPacket.model_validate_json(args.packet.read_text(encoding="utf-8"))
        decisions = _DECISIONS.validate_json(args.decisions.read_text(encoding="utf-8"))
        report = freeze_reviewed_dataset(
            args.root,
            packet=packet,
            decisions=decisions,
            dataset_version=args.dataset_version,
            allow_pc_openai=args.allow_pc_openai,
            output_root=None if args.in_place else args.output_root,
        )
        print(
            f"PASS version={report.dataset_version} bundles={report.bundle_count} "
            f"reviewer={report.reviewer} revision={report.review_revision} "
            f"external_provider_allowed={str(report.external_provider_allowed).lower()} "
            f"output={report.output_root}"
        )
        return 0
    except (OSError, ValueError, ValidationError) as error:
        print(
            f"FAIL operation={args.command} error_class={type(error).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
