"""Validate offline bindings, stable credentials and observability rollout failure gates."""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

from scripts.deploy import prepare_observability_secrets, render_k8s

ROOT = Path(__file__).resolve().parents[3]


def images(mode="standalone"):
    roles = {"backend", "frontend", "postgres"} | render_k8s.OBSERVABILITY_ROLES
    if mode == "standalone":
        roles |= {"prometheus", "grafana"}
    return {role: f"registry.vlp.vn/kira/{role}@sha256:{'b' * 64}" for role in roles}


@pytest.mark.parametrize("mode", ["shared", "standalone"])
def test_render_selected_stack_without_copying_private_files(tmp_path, mode):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "deploy/k8s", source)
    (source / "observability/private.yaml").write_text("private-canary")
    output = tmp_path / "ready"
    references = images(mode)
    render_k8s.render(references, output, source, mode)
    assert not (output / "observability/private.yaml").exists()
    assert (output / "observability/metrics.yaml").exists() == (mode == "standalone")
    assert (output / "observability/mode").read_text().strip() == mode
    assert json.loads((output / "release-images.json").read_text()) == references
    emitted = set()
    for path in output.rglob("*.yaml"):
        text = path.read_text()
        assert not render_k8s.re.search(r"REPLACE_[A-Z_]+_IMAGE", text)
        for resource in yaml.safe_load_all(text):
            if resource["kind"] in {"Deployment", "StatefulSet", "Job"}:
                emitted.update(
                    c["image"] for c in resource["spec"]["template"]["spec"]["containers"]
                )
    assert emitted == set(references.values())


def test_incomplete_observability_binding_fails_before_output(tmp_path):
    references = images()
    del references["langfuse_worker"]
    with pytest.raises(ValueError, match="langfuse_worker"):
        render_k8s.render(references, tmp_path / "ready", observability="standalone")
    assert not (tmp_path / "ready").exists()


def test_credentials_align_datastores_project_auth_and_template():
    resource = prepare_observability_secrets.document("operator@example.invalid")
    values = resource["stringData"]
    expected = yaml.safe_load((ROOT / "deploy/k8s/observability/secrets.example.yaml").read_text())[
        "stringData"
    ]
    assert set(values) == set(expected)
    url = urlsplit(values["LF_DATABASE_URL"])
    assert (url.username, url.hostname, url.port, url.path) == (
        "langfuse",
        "langfuse-postgres",
        5432,
        "/langfuse",
    )
    assert unquote(url.password) == values["LF_PG_PASSWORD"]
    assert parse_qs(url.query) == {"connection_limit": ["15"], "pool_timeout": ["10"]}
    assert values["LF_PG_ADMIN_PASSWORD"] != values["LF_PG_PASSWORD"]
    assert len(bytes.fromhex(values["LANGFUSE_ENCRYPTION_KEY"])) == 32
    auth = base64.b64decode(values["LANGFUSE_OTEL_AUTH"].removeprefix("Basic ")).decode()
    assert (
        auth == f"{values['LANGFUSE_PROJECT_PUBLIC_KEY']}:{values['LANGFUSE_PROJECT_SECRET_KEY']}"
    )
    assert (
        values != prepare_observability_secrets.document("operator@example.invalid")["stringData"]
    )
    # Langfuse migration identity is isolated from core app/runtime credentials.
    assert not {"KIRA_BASIC_AUTH", "DATABASE_URL", "MEMORY_DATABASE_URL"} & set(values)


def test_secret_generator_refuses_rotation_over_existing_pvcs(tmp_path):
    output = tmp_path / "secret.yaml"
    output.write_text("stable-credentials")
    with pytest.raises(FileExistsError, match="existing PVCs"):
        prepare_observability_secrets.prepare("operator@example.invalid", output)
    assert output.read_text() == "stable-credentials"


@pytest.mark.parametrize("failure", ["none", "datastore", "bucket", "langfuse", "collector"])
def test_telemetry_enabled_only_after_backend_rollout_and_bucket_success(tmp_path, failure):
    shell = shutil.which("sh")
    if shell is None:
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        shell = str(candidate) if candidate.exists() else None
    if shell is None:
        pytest.skip("POSIX shell required to exercise the real rollout failure gate")
    release = tmp_path / "ready"
    render_k8s.render(images(), release, observability="standalone")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"

    def posix(path):
        value = path.as_posix()
        return f"/{value[0].lower()}{value[2:]}" if path.drive else value

    (fake_bin / "kubectl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$KIRA_TEST_LOG"\n'
        'case "$*" in\n'
        ' *"rollout status statefulset/langfuse-postgres"*) '
        '[ "$KIRA_TEST_FAILURE" != datastore ] || exit 1;;\n'
        ' *"rollout status deployment/langfuse-web"*) '
        '[ "$KIRA_TEST_FAILURE" != langfuse ] || exit 1;;\n'
        ' *"rollout status deployment/otel-collector"*) '
        '[ "$KIRA_TEST_FAILURE" != collector ] || exit 1;;\n'
        ' *"get job langfuse-s3-init"*) '
        'if [ "$KIRA_TEST_FAILURE" = bucket ]; then printf "|1|True"; '
        'else printf "1||"; fi;;\nesac\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    (fake_bin / "kubectl").chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_text(
        f'export PATH="{posix(fake_bin)}:/usr/bin:/bin"\n'
        f'export KIRA_TEST_LOG="{posix(log)}"\n'
        f'exec sh "{posix(release / "observability/apply.sh")}" apply\n',
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        [shell, str(runner)],
        env={**os.environ, "KIRA_TEST_FAILURE": failure},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert (result.returncode == 0) == (failure == "none"), result.stderr
    calls = log.read_text()
    assert ("patch configmap kira-runtime-config" in calls) == (failure == "none")
    assert ("rollout restart deployment/gateway" in calls) == (failure == "none")
    if failure == "none":
        declared = {
            f"{resource['kind'].lower()}/{resource['metadata']['name']}"
            for path in release.rglob("*.yaml")
            for resource in yaml.safe_load_all(path.read_text())
            if resource["kind"] in {"Deployment", "StatefulSet"}
        }
        for call in calls.splitlines():
            if "rollout status" in call:
                target = call.split("rollout status ", 1)[1].split()[0]
                assert target in declared, f"Rollout waits for an undeclared workload: {target}"
        ordered = [
            "datastores.yaml",
            "get job langfuse-s3-init",
            "langfuse.yaml",
            "collector.yaml",
            "metrics.yaml",
            "patch configmap kira-runtime-config",
            "rollout restart deployment/gateway",
        ]
        positions = [calls.index(part) for part in ordered]
        assert positions == sorted(positions)
