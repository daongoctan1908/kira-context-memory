"""Exercise real snapshot/export integrity boundaries without a Docker daemon."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.deploy import offline_images as handoff


@pytest.fixture
def prepared(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    names = (
        handoff.ROOT_INPUTS
        | set(handoff.HANDOFF_FILES)
        | {
            "frontend/Dockerfile",
            "frontend/package.json",
            "frontend/pnpm-lock.yaml",
            "scripts/deploy/OFFLINE-IMAGES.md",
            "app/main.py",
            ".gitignore",
        }
    )
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content: {name}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nversion="0.4.1"\n', encoding="utf-8")
    (root / ".gitignore").write_text(".env*\n", encoding="utf-8")
    handoff.run("git", "-C", str(root), "init", "--quiet")
    handoff.run("git", "-C", str(root), "add", ".")
    handoff.run(
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    # An ignored credential and a tracked credential must both be absent from build contexts.
    (root / ".env.local").write_text("SECRET=synthetic-canary\n", encoding="utf-8")
    (root / "frontend/.env.local").write_text("SECRET=synthetic-canary\n", encoding="utf-8")
    handoff.run("git", "-C", str(root), "add", "-f", "frontend/.env.local")
    (root / "app/main.py").write_text("# uncommitted source change\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    handoff.prepare(bundle, root)
    return bundle


def fake_docker(monkeypatch: pytest.MonkeyPatch, arch: str = "amd64") -> list[tuple[str, ...]]:
    calls = []
    labels = {}

    def invoke(*args: str, capture: bool = False) -> str:
        assert args[0] == "docker"
        calls.append(args)
        if args[1] == "info":
            return "linux"
        if args[1:3] == ("buildx", "build"):
            reference = args[args.index("-t") + 1]
            labels[reference] = {
                args[index + 1].split("=", 1)[0]: args[index + 1].split("=", 1)[1]
                for index, value in enumerate(args)
                if value == "--label"
            }
        elif args[1:3] == ("image", "inspect"):
            reference = args[-1]
            return json.dumps(
                [
                    {
                        "Os": "linux",
                        "Architecture": arch,
                        "Id": "sha256:" + "a" * 64,
                        "RepoDigests": [],
                        "Config": {"User": "10001", "Labels": labels.get(reference, {})},
                    }
                ]
            )
        elif args[1:3] == ("image", "save"):
            path = Path(args[args.index("--output") + 1])
            with tarfile.open(path, "w") as archive:
                config = b'{"os":"linux","architecture":"amd64"}'
                manifest = json.dumps(
                    [
                        {
                            "Config": "config.json",
                            "RepoTags": [args[-1]],
                            "Layers": [],
                        }
                    ]
                ).encode()
                for name, content in (("config.json", config), ("manifest.json", manifest)):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        else:
            assert args[1] in {"pull", "run"}
        return ""

    monkeypatch.setattr(handoff, "run", invoke)
    return calls


def test_prepare_freezes_uncommitted_source_without_credentials(prepared: Path) -> None:
    source = json.loads((prepared / "source-manifest.json").read_text(encoding="utf-8"))
    names = {record["path"] for record in source["files"]}
    assert ".env.local" not in names
    assert "frontend/.env.local" not in names
    assert (prepared / "build-context/app/main.py").read_text() == "# uncommitted source change\n"
    assert source["source_state"] == "working_tree_snapshot"
    with pytest.raises(FileExistsError):
        handoff.prepare(prepared, prepared.parent / "repo")


def test_changed_snapshot_is_rejected_before_docker(prepared: Path, monkeypatch) -> None:
    (prepared / "build-context/app/main.py").write_text("# changed after freeze\n")
    monkeypatch.setattr(handoff, "run", lambda *args, **kwargs: pytest.fail("Docker must not run"))
    with pytest.raises(ValueError, match="Frozen build context changed"):
        handoff.build(prepared)


def test_bundle_contains_all_product_roles_and_rejects_transfer_damage(prepared: Path, monkeypatch):
    calls = fake_docker(monkeypatch)
    handoff.build(prepared)
    transfer = prepared / "transfer"
    handoff.verify(transfer)
    manifest = json.loads((transfer / "image-manifest.json").read_text())
    assert {record["role"] for record in manifest["images"]} == handoff.ROLES
    assert manifest["application_acceptance"] == "NOT_RUN"
    assert all(record["config_digest"] != record["image_id"] for record in manifest["images"])
    builds = [args for args in calls if args[1:3] == ("buildx", "build")]
    assert len(builds) == 2
    assert all(args[args.index("--platform") + 1] == "linux/amd64" for args in builds)
    (transfer / "frontend.tar").write_bytes(b"corrupted in transfer")
    with pytest.raises(ValueError, match="Checksum mismatch: frontend.tar"):
        handoff.verify(transfer)


def test_foreign_architecture_cannot_be_exported(prepared: Path, monkeypatch):
    calls = fake_docker(monkeypatch, arch="arm64")
    with pytest.raises(ValueError, match="Unexpected image architecture"):
        handoff.build(prepared)
    assert not any(args[1:3] == ("image", "save") for args in calls)
    assert not (prepared / "transfer/image-manifest.json").exists()
