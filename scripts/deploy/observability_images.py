"""Export reviewed observability images as verifiable linux/amd64 Docker archives."""

import argparse
import json
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.deploy.offline_images import PLATFORM, archive_config_digest, run, sha256, write_json

# Keep Langfuse on the locally accepted v3 release; a v4 upgrade needs new acceptance.
IMAGES = {
    "otel_collector": (
        "otel/opentelemetry-collector-contrib:0.160.0"
        "@sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6"
    ),
    "langfuse_web": (
        "langfuse/langfuse:3.225.7"
        "@sha256:a27fe525f52984fa6d36fd34e8b8c6e5ae4af43f34134cafc833b467fa9580ae"
    ),
    "langfuse_worker": (
        "langfuse/langfuse-worker:3.225.7"
        "@sha256:93207bd67d2e789ea55fa3d47eeba065dd6b1869187127ab24201784c52b96bc"
    ),
    "langfuse_postgres": "postgres:17",
    "langfuse_clickhouse": "clickhouse/clickhouse-server:25.12",
    "langfuse_redis": "redis:7",
    "langfuse_minio": (
        "cgr.dev/chainguard/minio"
        "@sha256:21b9e1bccf46098dee2e928952bf5fd4690a5ec30a3b86a1e728f1bb0ceaed0e"
    ),
    "prometheus": "prom/prometheus:v3.14.0",
    "grafana": "grafana/grafana:13.2.1",
}
MODES = {
    "langfuse": tuple(role for role in IMAGES if role not in {"prometheus", "grafana"}),
    "standalone": tuple(IMAGES),
}
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
MANIFEST = "image-manifest.json"
CHECKSUMS = "SHA256SUMS.txt"


def _roles(mode: str) -> tuple[str, ...]:
    if mode not in MODES:
        raise ValueError("Mode must be langfuse or standalone")
    return MODES[mode]


def _archive_platform(path: Path, reference: str) -> str:
    """Inspect the selected tar config directly, without extracting archive members."""
    with tarfile.open(path) as archive:
        stream = archive.extractfile("manifest.json")
        if stream is None:
            raise ValueError("Image archive has no Docker manifest")
        entries = json.load(stream)
        matches = [entry for entry in entries if reference in (entry.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("Image archive does not identify its expected tag")
        config_stream = archive.extractfile(matches[0]["Config"])
        if config_stream is None:
            raise ValueError("Image archive has no configuration blob")
        config = json.load(config_stream)
        return f"{config.get('os')}/{config.get('architecture')}"


def _inspect(role: str, source: str) -> dict[str, object]:
    response = json.loads(run("docker", "image", "inspect", source, capture=True))
    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
        raise ValueError(f"Invalid Docker inspect response: {role}")
    info = response[0]
    if f"{info.get('Os')}/{info.get('Architecture')}" != PLATFORM:
        raise ValueError(f"Unexpected image architecture: {role}")
    identity = info.get("Id", "")
    if not isinstance(identity, str) or not DIGEST.fullmatch(identity):
        raise ValueError(f"Invalid Docker image identity: {role}")
    digests = info.get("RepoDigests") or []
    if not digests or any(
        not isinstance(value, str) or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value)
        for value in digests
    ):
        raise ValueError(f"Missing or invalid upstream registry identity: {role}")
    if "@sha256:" in source and not any(
        value.endswith("@" + source.rsplit("@", 1)[1]) for value in digests
    ):
        raise ValueError(f"Pinned upstream digest mismatch: {role}")
    return {
        "role": role,
        "source_reference": source,
        # Saving a tagged reference gives both Docker image stores a portable RepoTags entry.
        "reference": f"kira-context-memory/observability/{role}:offline-{identity[7:19]}",
        "image_id": identity,
        "repo_digests": digests,
        "platform": PLATFORM,
        "archive": f"{role}.tar",
    }


def export(output: Path, mode: str = "standalone") -> None:
    """Pull and save backend images; never build the app, zip, push or apply manifests."""
    roles = _roles(mode)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Output already exists; use a new directory for this export")
    if run("docker", "info", "--format", "{{.OSType}}", capture=True) != "linux":
        raise ValueError("Docker must be running with a Linux container engine")

    images = []
    for role in roles:
        source = IMAGES[role]
        run("docker", "pull", "--platform", PLATFORM, source)
        images.append(_inspect(role, source))

    # Complete all architecture/identity checks before creating or saving the transfer directory.
    output.mkdir(parents=True, exist_ok=False)
    for image in images:
        reference = str(image["reference"])
        archive = output / str(image["archive"])
        # Tag the inspected immutable local ID, not a source tag that another pull could update.
        run("docker", "image", "tag", str(image["image_id"]), reference)
        run("docker", "image", "save", "--platform", PLATFORM, "--output", str(archive), reference)
        if _archive_platform(archive, reference) != PLATFORM:
            raise ValueError(f"Unexpected image archive architecture: {image['role']}")
        image["config_digest"] = archive_config_digest(archive, reference)
        image["archive_sha256"] = sha256(archive)
        image["archive_size_bytes"] = archive.stat().st_size

    # No completion manifest/checksum set is written for a failed or partial export.
    write_json(
        output / MANIFEST,
        {
            "schema_version": 1,
            "kind": "kira-observability-images",
            "status": "PULLED_EXPORTED",
            "mode": mode,
            "platform": PLATFORM,
            "created_at": datetime.now(UTC).isoformat(),
            "images": images,
            "container_smoke": "NOT_RUN",
            "application_acceptance": "NOT_RUN",
            "registry_publish": "NOT_RUN",
            "kubernetes_acceptance": "NOT_RUN",
        },
    )
    names = sorted([MANIFEST, *(str(image["archive"]) for image in images)])
    (output / CHECKSUMS).write_text(
        "".join(f"{sha256(output / name)}  {name}\n" for name in names),
        encoding="utf-8",
        newline="\n",
    )
    verify(output)
    print(f"Raw observability archives ready: {output}")


def verify(output: Path) -> None:
    """Check transferred archive identities and platform without Docker or network access."""
    output = output.resolve()
    checked = set()
    for line in (output / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        if not match:
            raise ValueError("Invalid checksum entry")
        digest, name = match.groups()
        path = (output / name).resolve()
        if name in checked or not path.is_relative_to(output) or path == output:
            raise ValueError("Unsafe or duplicate checksum path")
        if sha256(path) != digest:
            raise ValueError(f"Checksum mismatch: {name}")
        checked.add(name)
    if MANIFEST not in checked:
        raise ValueError("Missing checked image manifest")
    manifest = json.loads((output / MANIFEST).read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "kira-observability-images"
        or manifest.get("platform") != PLATFORM
        or manifest.get("status") != "PULLED_EXPORTED"
    ):
        raise ValueError("Unexpected observability export contract/platform")
    roles = _roles(manifest["mode"])
    images = manifest["images"]
    if len(images) != len(roles) or {image["role"] for image in images} != set(roles):
        raise ValueError("Missing or duplicate observability image roles")
    expected = {MANIFEST, *(f"{role}.tar" for role in roles)}
    if checked != expected:
        raise ValueError("Incomplete or unexpected checksum coverage")
    for image in images:
        role = image["role"]
        if image["archive"] != f"{role}.tar" or image["source_reference"] != IMAGES[role]:
            raise ValueError(f"Invalid image reference/archive: {role}")
        if image["platform"] != PLATFORM or not DIGEST.fullmatch(image["image_id"]):
            raise ValueError(f"Invalid image identity/platform: {role}")
        expected_reference = (
            f"kira-context-memory/observability/{role}:offline-{image['image_id'][7:19]}"
        )
        if image["reference"] != expected_reference:
            raise ValueError(f"Invalid local export reference: {role}")
        archive = output / image["archive"]
        if sha256(archive) != image["archive_sha256"]:
            raise ValueError(f"Archive does not match its image manifest: {role}")
        if _archive_platform(archive, expected_reference) != PLATFORM:
            raise ValueError(f"Unexpected image archive architecture: {role}")
        if archive_config_digest(archive, expected_reference) != image["config_digest"]:
            raise ValueError(f"Image configuration identity mismatch: {role}")
    print("Observability archive SHA256 and linux/amd64 verification: PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "verify"))
    parser.add_argument("--output", type=Path, required=True, help="Raw archive transfer directory")
    parser.add_argument("--mode", choices=tuple(MODES), default="standalone")
    args = parser.parse_args(argv)
    try:
        if args.action == "export":
            export(args.output, args.mode)
        else:
            verify(args.output)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Observability image handoff failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
