"""Bind published internal-registry digests to the reviewable Kubernetes YAML."""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy/k8s"
IMAGE_PATTERN = re.compile(r"registry\.vlp\.vn/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}")
PUBLIC_FILES = (
    "namespace.yaml",
    "config.yaml",
    "postgres-pvc.yaml",
    "postgres.yaml",
    "networkpolicy.yaml",
    "runtime.yaml",
    "jobs/migrate.yaml",
    "jobs/memory-init.yaml",
    "jobs/memory-validate.yaml",
    "optional/operator.yaml",
    "optional/frontend-nodeport.yaml",
    "secrets.example.yaml",
    "apply.sh",
    "README.md",
    "DEPLOY.md",
    "CAPACITY.md",
)
OBSERVABILITY_FILES = (
    "observability/config.yaml",
    "observability/datastores.yaml",
    "observability/s3-init.yaml",
    "observability/langfuse.yaml",
    "observability/collector.yaml",
    "observability/networkpolicy.yaml",
    "observability/secrets.example.yaml",
    "observability/apply.sh",
    "observability/README.md",
)
OBSERVABILITY_ROLES = {
    "otel_collector",
    "langfuse_web",
    "langfuse_worker",
    "langfuse_postgres",
    "langfuse_clickhouse",
    "langfuse_redis",
    "langfuse_minio",
}


def render(
    images: dict[str, str],
    output: Path,
    source: Path = TEMPLATES,
    observability: str = "none",
) -> None:
    if not isinstance(images, dict):
        raise ValueError("Images must be a JSON object mapping roles to published digests")
    if observability not in {"none", "shared", "standalone"}:
        raise ValueError("Observability must be none, shared or standalone")
    required = {"backend", "frontend", "postgres"}
    names = list(PUBLIC_FILES)
    if observability != "none":
        required |= OBSERVABILITY_ROLES
        names.extend(OBSERVABILITY_FILES)
    if observability == "standalone":
        required |= {"prometheus", "grafana"}
        names.append("observability/metrics.yaml")
    if set(images) != required:
        raise ValueError(f"Images must declare exactly: {', '.join(sorted(required))}")
    if any(not isinstance(v, str) or not IMAGE_PATTERN.fullmatch(v) for v in images.values()):
        raise ValueError("Use published registry.vlp.vn repository@sha256 digests for every image")
    # Collect first so a missing input or invalid template cannot leave an apparently ready output.
    files = []
    for name in names:
        path = source / name
        text = path.read_text(encoding="utf-8")
        for role, reference in images.items():
            text = text.replace(f"REPLACE_{role.upper()}_IMAGE", reference)
        if re.search(r"REPLACE_[A-Z_]+_IMAGE", text):
            raise ValueError(f"Unresolved image marker: {path.name}")
        files.append((path.relative_to(source), text))
    output.mkdir(parents=True, exist_ok=False)
    for relative, text in files:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    shutil.copymode(source / "apply.sh", output / "apply.sh")
    if observability != "none":
        shutil.copymode(source / "observability/apply.sh", output / "observability/apply.sh")
        (output / "observability/mode").write_text(observability + "\n", encoding="utf-8")
    (output / "release-images.json").write_text(
        json.dumps(images, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"Bound Kubernetes YAML ready for review: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observability", choices=("none", "shared", "standalone"), default="none")
    args = parser.parse_args()
    try:
        render(
            json.loads(args.images.read_text(encoding="utf-8")),
            args.output.resolve(),
            observability=args.observability,
        )
    except (OSError, ValueError, TypeError) as error:
        print(f"Kubernetes render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
