"""Freeze product build inputs, build amd64 images, and export a verifiable offline bundle."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "linux/amd64"
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16-bookworm"
ROLES = {"backend", "frontend", "postgres"}
ROOT_INPUTS = {
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "alembic.ini",
    "packages/viettel-mem0/pyproject.toml",
    "packages/viettel-mem0/README.md",
    "packages/viettel-mem0/LICENSE",
}
SOURCE_PREFIXES = ("app/", "worker/", "migrations/", "packages/viettel-mem0/mem0/", "frontend/")
HANDOFF_FILES = (
    "deploy/k8s/namespace.yaml",
    "deploy/k8s/postgres-pvc.yaml",
    "deploy/production-rewrite.env.example",
    "deploy/production-memory.env.example",
)


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_config_digest(path: Path, reference: str) -> str:
    """Read the config identity shared by classic and containerd Docker image stores."""
    with tarfile.open(path) as archive:
        stream = archive.extractfile("manifest.json")
        if stream is None:
            raise ValueError("Image archive has no Docker manifest")
        entries = json.load(stream)
        matches = [entry for entry in entries if reference in (entry.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("Image archive does not identify its expected tag")
        config = archive.extractfile(matches[0]["Config"])
        if config is None:
            raise ValueError("Image archive has no configuration blob")
        return "sha256:" + hashlib.file_digest(config, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_input(name: str) -> bool:
    parts = Path(name).parts
    if any(
        p.startswith(".env")
        or p
        in {
            "node_modules",
            "dist",
            "build",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "coverage",
            "test-results",
            "playwright-report",
            "blob-report",
        }
        for p in parts
    ) or name.endswith((".pyc", ".pyo", ".log")):
        return False
    return name in ROOT_INPUTS or name.startswith(SOURCE_PREFIXES)


def prepare(bundle: Path, root: Path = ROOT) -> None:
    """Copy a frozen, credential-free source context; never overwrite an earlier bundle."""
    revision = run("git", "-C", str(root), "rev-parse", "HEAD", capture=True)
    names = run(
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        capture=True,
    ).split("\0")
    names = sorted({n for n in names if n and source_input(n) and (root / n).is_file()})
    required = ROOT_INPUTS | {
        "frontend/Dockerfile",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
    }
    if missing := required - set(names):
        raise ValueError(f"Missing build inputs: {sorted(missing)}")
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Application version must be a numeric release version")
    for name in (*HANDOFF_FILES, "scripts/deploy/OFFLINE-IMAGES.md"):
        if not (root / name).is_file():
            raise ValueError(f"Missing non-secret deployment fragment: {name}")
    bundle.mkdir(parents=True, exist_ok=False)
    context = bundle / "build-context"
    records = []
    for name in names:
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
        records.append({"path": name, "sha256": sha256(target)})
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    source = {
        "schema_version": 1,
        "base_revision": revision,
        "source_state": "working_tree_snapshot",
        "snapshot_sha256": hashlib.sha256(canonical).hexdigest(),
        "app_version": version,
        "platform": PLATFORM,
        "files": records,
    }
    write_json(bundle / "source-manifest.json", source)
    transfer = bundle / "transfer"
    transfer.mkdir()
    for name in HANDOFF_FILES:
        target = transfer / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    shutil.copyfile(root / "scripts/deploy/OFFLINE-IMAGES.md", transfer / "HANDOFF.md")
    print(f"Prepared frozen build inputs: {bundle}\nImages have not been built.")


def build(bundle: Path) -> None:
    if not bundle.exists():
        prepare(bundle)
    source_path = bundle / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    canonical = json.dumps(source["files"], sort_keys=True, separators=(",", ":")).encode()
    if (
        source["platform"] != PLATFORM
        or hashlib.sha256(canonical).hexdigest() != source["snapshot_sha256"]
    ):
        raise ValueError("Invalid frozen source manifest")
    context, transfer = bundle / "build-context", bundle / "transfer"
    if (transfer / "image-manifest.json").exists():
        raise ValueError("Bundle is complete; use a new directory for a new release")
    for record in source["files"]:
        if sha256(context / record["path"]) != record["sha256"]:
            raise ValueError("Frozen build context changed; prepare a new bundle")
    engine = run("docker", "info", "--format", "{{.OSType}}", capture=True)
    if engine != "linux":
        raise ValueError("Docker must be running with a Linux container engine")
    tag = f"{source['app_version']}-{source['snapshot_sha256'][:12]}"
    references = {}
    for role in ("backend", "frontend"):
        references[role] = f"kira-context-memory/{role}:{tag}"
        build_context = context if role == "backend" else context / "frontend"
        args = [
            "docker",
            "buildx",
            "build",
            "--platform",
            PLATFORM,
            "--load",
            "--progress",
            "plain",
            "--build-arg",
            f"APP_VERSION={source['app_version']}",
            "--build-arg",
            f"SOURCE_REVISION={source['base_revision']}",
            "--label",
            f"io.kira.build.source-sha256={source['snapshot_sha256']}",
            "--label",
            f"io.kira.release.role={role}",
            "--metadata-file",
            str(bundle / f"{role}-build-metadata.json"),
            "-t",
            references[role],
            str(build_context),
        ]
        run(*args)
    run("docker", "pull", "--platform", PLATFORM, POSTGRES_IMAGE)
    references["postgres"] = POSTGRES_IMAGE
    images = []
    for role, reference in references.items():
        info = json.loads(run("docker", "image", "inspect", reference, capture=True))[0]
        if f"{info['Os']}/{info['Architecture']}" != PLATFORM:
            raise ValueError(f"Unexpected image architecture: {role}")
        if role != "postgres":
            labels = info["Config"]["Labels"]
            if labels.get("io.kira.build.source-sha256") != source["snapshot_sha256"]:
                raise ValueError(f"Image provenance mismatch: {role}")
            if info["Config"].get("User") in (None, "", "0", "root", "0:0"):
                raise ValueError(f"Application image must run as non-root: {role}")
        images.append(
            {
                "role": role,
                "reference": reference,
                "image_id": info["Id"],
                "platform": PLATFORM,
                "repo_digests": info.get("RepoDigests") or [],
                "archive": f"{role}.tar",
            }
        )
    # These checks need no production credentials or external model access.
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        references["backend"],
        "-c",
        "from importlib.metadata import version; import app.presentation.api.main; "
        "import worker.main; import alembic; "
        f"assert version('kira-context-memory') == {source['app_version']!r}; "
        "print('Gateway/Worker/migrations imports: PASS')",
    )
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-e",
        "GATEWAY_UPSTREAM=127.0.0.1:8000",
        references["frontend"],
        "nginx",
        "-t",
    )
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "sh",
        references["postgres"],
        "-ec",
        "postgres --version; test -f /usr/share/postgresql/16/extension/vector.control",
    )
    for image in images:
        archive = transfer / image["archive"]
        run("docker", "image", "save", "--output", str(archive), image["reference"])
        image["archive_sha256"] = sha256(archive)
        image["config_digest"] = archive_config_digest(archive, image["reference"])
    shutil.copyfile(source_path, transfer / "source-manifest.json")
    manifest = {
        "schema_version": 1,
        "status": "BUILT_EXPORTED",
        "platform": PLATFORM,
        "created_at": datetime.now(UTC).isoformat(),
        "images": images,
        "source_snapshot_sha256": source["snapshot_sha256"],
        "container_smoke": "PASS",
        "application_acceptance": "NOT_RUN",
        "registry_publish": "NOT_RUN",
    }
    write_json(transfer / "image-manifest.json", manifest)
    entries = [
        f"{sha256(path)}  {path.relative_to(transfer).as_posix()}"
        for path in sorted(transfer.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS.txt"
    ]
    (transfer / "SHA256SUMS.txt").write_text(
        "\n".join(entries) + "\n", encoding="utf-8", newline="\n"
    )
    verify(transfer)
    print(f"Offline images ready. Transfer this directory: {transfer}")


def verify(transfer: Path) -> None:
    """Verify transferred files without requiring Docker, network access or credentials."""
    checked = set()
    for line in (transfer / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        if not match:
            raise ValueError("Invalid checksum entry")
        digest, name = match.groups()
        path = (transfer / name).resolve()
        if not path.is_relative_to(transfer.resolve()) or name in checked:
            raise ValueError("Unsafe or duplicate checksum path")
        if sha256(path) != digest:
            raise ValueError(f"Checksum mismatch: {name}")
        checked.add(name)
    if not {"image-manifest.json", "source-manifest.json", *HANDOFF_FILES, "HANDOFF.md"} <= checked:
        raise ValueError("Incomplete offline bundle")
    manifest = json.loads((transfer / "image-manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1 or manifest["platform"] != PLATFORM:
        raise ValueError("Unexpected bundle contract/platform")
    if {i["role"] for i in manifest["images"]} != ROLES or len(manifest["images"]) != 3:
        raise ValueError("Product bundle must contain backend, frontend and postgres")
    for image in manifest["images"]:
        if image["archive"] != f"{image['role']}.tar" or image["archive"] not in checked:
            raise ValueError("Missing or invalid image archive")
        if image["platform"] != PLATFORM or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", image["image_id"]
        ):
            raise ValueError("Invalid image identity/platform")
        if sha256(transfer / image["archive"]) != image["archive_sha256"]:
            raise ValueError("Archive does not match its image manifest")
        if (
            archive_config_digest(transfer / image["archive"], image["reference"])
            != image["config_digest"]
        ):
            raise ValueError("Image configuration identity mismatch")
    print("Offline bundle SHA256 verification: PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "build", "verify"))
    parser.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="Build directory, or the transfer directory for verify",
    )
    args = parser.parse_args()
    try:
        {"prepare": prepare, "build": build, "verify": verify}[args.action](args.bundle.resolve())
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Image handoff failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
