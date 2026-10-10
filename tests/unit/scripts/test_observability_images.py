"""Exercise the raw observability image handoff without pulling or exporting real images."""

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.deploy import observability_images as handoff


def _archive(path: Path, reference: str, arch: str) -> None:
    config = json.dumps({"os": "linux", "architecture": arch}).encode()
    manifest = json.dumps(
        [{"Config": "config.json", "RepoTags": [reference], "Layers": []}]
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, content in (("config.json", config), ("manifest.json", manifest)):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inspect_arch: str = "amd64",
    archive_arch: str = "amd64",
    fail_export: str | None = None,
    mismatch_digest: bool = False,
) -> list[tuple[str, ...]]:
    calls = []
    source_roles = {source: role for role, source in handoff.IMAGES.items()}
    identities = {
        role: "sha256:" + hashlib.sha256(role.encode()).hexdigest() for role in handoff.IMAGES
    }
    tags = {}

    def invoke(*args: str, capture: bool = False) -> str:
        calls.append(args)
        assert args[0] == "docker"
        if args[1] == "info":
            assert capture
            return "linux"
        if args[1] == "pull":
            assert args[2:4] == ("--platform", "linux/amd64")
            assert args[4] in source_roles
        elif args[1:3] == ("image", "inspect"):
            role = source_roles[args[-1]]
            source = args[-1]
            digest = source.rsplit("@", 1)[1] if "@sha256:" in source else identities[role]
            if mismatch_digest:
                digest = "sha256:" + "b" * 64
            return json.dumps(
                [
                    {
                        "Os": "linux",
                        "Architecture": inspect_arch,
                        "Id": identities[role],
                        "RepoDigests": [source.split("@", 1)[0] + "@" + digest],
                    }
                ]
            )
        elif args[1:3] == ("image", "tag"):
            assert args[3] in identities.values()
            tags[args[4]] = next(role for role, value in identities.items() if value == args[3])
        elif args[1:3] == ("image", "save"):
            assert args[3:5] == ("--platform", "linux/amd64")
            role = tags[args[-1]]
            if role == fail_export:
                raise subprocess.CalledProcessError(1, args)
            _archive(Path(args[args.index("--output") + 1]), args[-1], archive_arch)
        else:
            pytest.fail(f"Unexpected mutation: {args}")
        return ""

    monkeypatch.setattr(handoff, "run", invoke)
    return calls


@pytest.mark.parametrize("mode,count", [("langfuse", 7), ("standalone", 9)])
def test_export_covers_requested_roles_and_can_verify_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, count: int
) -> None:
    calls = fake_docker(monkeypatch)
    output = tmp_path / mode
    handoff.export(output, mode)
    manifest = json.loads((output / handoff.MANIFEST).read_text())
    assert manifest["mode"] == mode
    assert {image["role"] for image in manifest["images"]} == set(handoff.MODES[mode])
    assert len(manifest["images"]) == count
    assert len(list(output.glob("*.tar"))) == count
    assert not list(output.glob("*.zip"))
    assert manifest["registry_publish"] == "NOT_RUN"
    assert manifest["container_smoke"] == "NOT_RUN"
    assert manifest["kubernetes_acceptance"] == "NOT_RUN"
    assert all(image["image_id"] != image["config_digest"] for image in manifest["images"])
    assert len([args for args in calls if args[1] == "pull"]) == count
    assert len([args for args in calls if args[1:3] == ("image", "save")]) == count
    monkeypatch.setattr(handoff, "run", lambda *args, **kwargs: pytest.fail("Docker must not run"))
    handoff.verify(output)


def test_existing_output_is_rejected_before_any_docker_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "reviewed"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("reviewed original")
    monkeypatch.setattr(handoff, "run", lambda *args, **kwargs: pytest.fail("Docker must not run"))
    with pytest.raises(FileExistsError, match="Output already exists"):
        handoff.export(output)
    assert marker.read_text() == "reviewed original"


def test_wrong_image_architecture_is_rejected_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_docker(monkeypatch, inspect_arch="arm64")
    output = tmp_path / "foreign"
    with pytest.raises(ValueError, match="Unexpected image architecture"):
        handoff.export(output)
    assert not output.exists()
    assert not any(args[1:3] == ("image", "save") for args in calls)


def test_pinned_upstream_digest_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker(monkeypatch, mismatch_digest=True)
    with pytest.raises(ValueError, match="Pinned upstream digest mismatch"):
        handoff.export(tmp_path / "mismatch")


@pytest.mark.parametrize("failure", ["docker", "archive_arch"])
def test_failed_or_wrong_platform_export_has_no_completion_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    fake_docker(
        monkeypatch,
        fail_export="langfuse_web" if failure == "docker" else None,
        archive_arch="arm64" if failure == "archive_arch" else "amd64",
    )
    output = tmp_path / "partial"
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        handoff.export(output)
    assert output.exists()
    assert not (output / handoff.MANIFEST).exists()
    assert not (output / handoff.CHECKSUMS).exists()


def test_transfer_damage_and_unsafe_checksum_paths_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker(monkeypatch)
    output = tmp_path / "transfer"
    handoff.export(output, "langfuse")
    (output / "langfuse_web.tar").write_bytes(b"damaged transfer")
    with pytest.raises(ValueError, match="Checksum mismatch: langfuse_web.tar"):
        handoff.verify(output)
    (output / handoff.CHECKSUMS).write_text("a" * 64 + "  ../outside.tar\n")
    with pytest.raises(ValueError, match="Unsafe or duplicate checksum path"):
        handoff.verify(output)


def test_config_identity_check_rejects_a_rewritten_manifest_with_updated_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker(monkeypatch)
    output = tmp_path / "identity"
    handoff.export(output, "langfuse")
    path = output / handoff.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["images"][0]["config_digest"] = "sha256:" + "f" * 64
    handoff.write_json(path, manifest)
    checksum_path = output / handoff.CHECKSUMS
    lines = checksum_path.read_text().splitlines()
    checksum_path.write_text(
        "\n".join(
            f"{handoff.sha256(path)}  {handoff.MANIFEST}"
            if line.endswith("  " + handoff.MANIFEST)
            else line
            for line in lines
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="Image configuration identity mismatch"):
        handoff.verify(output)


def test_invalid_mode_is_rejected_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handoff, "run", lambda *args, **kwargs: pytest.fail("Docker must not run"))
    with pytest.raises(ValueError, match="Mode must be"):
        handoff.export(tmp_path / "unknown", "unknown")
