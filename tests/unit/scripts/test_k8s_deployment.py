"""Check release binding, private credentials and failure gates before cluster rollout."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from app.config.settings import Settings
from scripts.deploy import prepare_k8s_secrets, render_k8s
from worker.settings import WorkerSettings

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "deploy/k8s"
IMAGES = {
    role: f"registry.vlp.vn/test/{role}@sha256:{'a' * 64}"
    for role in ("backend", "frontend", "postgres")
}
KIRA = {
    "KIRA_BASE_URL": "http://10.255.62.64:8122",
    "KIRA_DOMAIN": "VBI",
    "KIRA_USERNAME": "synthetic-service",
    "KIRA_BASIC_AUTH": "synthetic-basic-auth",
    "OPENAI_API_KEY": "synthetic-excluded-provider-key",
}


def resources(path: Path) -> list[dict]:
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def pod_spec(resource: dict) -> dict:
    return resource["spec"] if resource["kind"] == "Pod" else resource["spec"]["template"]["spec"]


def environment(resource: dict, bindings: dict[str, dict]) -> dict[str, str]:
    result = {}
    container = pod_spec(resource)["containers"][0]
    for source in container.get("envFrom", []):
        reference = source.get("configMapRef") or source["secretRef"]
        result.update(bindings[reference["name"]])
    for item in container.get("env", []):
        if "value" in item:
            value = item["value"]
            for name, existing in result.items():
                value = value.replace(f"$({name})", existing)
            result[item["name"]] = value
        elif "fieldRef" in item["valueFrom"]:
            assert item["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
            result[item["name"]] = "synthetic-pod-uid"
        else:
            reference = item["valueFrom"]["secretKeyRef"]
            result[item["name"]] = bindings[reference["name"]][reference["key"]]
    return result


def test_render_binds_all_images_and_never_copies_extra_private_files(tmp_path):
    source = tmp_path / "templates"
    shutil.copytree(TEMPLATES, source)
    (source / "private.yaml").write_text("secret: synthetic-canary\n")
    output = tmp_path / "release"
    render_k8s.render(IMAGES, output, source)
    assert not (output / "private.yaml").exists()
    workloads = []
    for path in output.rglob("*.yaml"):
        assert "REPLACE_BACKEND_IMAGE" not in path.read_text()
        workloads.extend(
            r for r in resources(path) if r["kind"] in {"Deployment", "StatefulSet", "Job", "Pod"}
        )
    assert {pod_spec(r)["containers"][0]["image"] for r in workloads} == set(IMAGES.values())
    assert json.loads((output / "release-images.json").read_text()) == IMAGES
    with pytest.raises(FileExistsError):
        render_k8s.render(IMAGES, output, source)


@pytest.mark.parametrize(
    "reference",
    [
        "registry.vlp.vn/test/backend:latest",
        "docker.io/test/backend@sha256:" + "a" * 64,
        "registry.vlp.vn/test/backend@sha256:short",
    ],
)
def test_render_rejects_unbound_images_before_writing(tmp_path, reference):
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="published"):
        render_k8s.render({**IMAGES, "backend": reference}, output)
    assert not output.exists()


def test_private_secrets_preserve_role_passwords_and_exclude_local_provider_keys():
    docs = prepare_k8s_secrets.documents(KIRA, "synthetic-pull", "synthetic-token")
    values = {r["metadata"]["name"]: r["stringData"] for r in docs}
    for key, role, password_key in (
        ("DATABASE_URL", "kira_app", "KIRA_APP_DB_PASSWORD"),
        ("MEMORY_DATABASE_URL", "kira_memory", "KIRA_MEMORY_DB_PASSWORD"),
    ):
        dsn = urlsplit(values["kira-database"][key])
        assert dsn.username == role
        assert unquote(dsn.password) == values["postgres-bootstrap"][password_key]
        assert (dsn.hostname, dsn.port, dsn.path) == ("postgres", 5432, "/kira_context")
    assert len(set(values["postgres-bootstrap"].values())) == 3
    for dsn_value in values["kira-database-admin"].values():
        dsn = urlsplit(dsn_value)
        assert dsn.username == "postgres"
        assert unquote(dsn.password) == values["postgres-bootstrap"]["POSTGRES_PASSWORD"]
    assert "synthetic-excluded-provider-key" not in yaml.safe_dump_all(docs)
    assert values["kira-service"] == {k: KIRA[k] for k in ("KIRA_USERNAME", "KIRA_BASIC_AUTH")}


def test_secret_preparation_refuses_to_replace_existing_credentials(tmp_path):
    output = tmp_path / "private.yaml"
    output.write_text("existing-credentials")
    with pytest.raises(FileExistsError, match="regenerate"):
        prepare_k8s_secrets.prepare(tmp_path / "missing.env", output)
    assert output.read_text() == "existing-credentials"


def test_manifests_satisfy_application_settings_without_admin_runtime_credentials():
    bindings = {
        r["metadata"]["name"]: r["data"]
        for r in resources(TEMPLATES / "config.yaml")
        if r["kind"] == "ConfigMap"
    }
    bindings.update(
        {
            r["metadata"]["name"]: r["stringData"]
            for r in prepare_k8s_secrets.documents(KIRA, None, None)
        }
    )
    runtimes = {
        r["metadata"]["name"]: r
        for r in resources(TEMPLATES / "runtime.yaml")
        if r["kind"] == "Deployment"
    }
    gateway_env = environment(runtimes["gateway"], bindings)
    worker_env = environment(runtimes["worker"], bindings)
    for values in (gateway_env, worker_env):
        assert "MEMORY_ADMIN_DATABASE_URL" not in values
        assert "POSTGRES_PASSWORD" not in values
        assert all("API_KEY" not in key for key in values)
        assert values["POD_UID"] == "synthetic-pod-uid"
        assert dict(
            pair.split("=", 1) for pair in values["OTEL_RESOURCE_ATTRIBUTES"].split(",")
        ) == {
            "service.instance.id": "synthetic-pod-uid",
            "k8s.pod.uid": "synthetic-pod-uid",
        }
    gateway = Settings(
        _env_file=None,
        **{k.lower(): v for k, v in gateway_env.items()},
        vllm_api_key=None,
        memory_embedding_api_key=None,
        memory_llm_api_key=None,
    )
    worker = WorkerSettings(
        _env_file=None,
        **{k.lower(): v for k, v in worker_env.items()},
        memory_embedding_api_key=None,
        memory_llm_api_key=None,
    )
    assert gateway.auth_enabled and not gateway.dev_static_identity_enabled
    assert gateway.memory_formation_enabled and gateway.memory_embedding_dims == 1024
    assert str(gateway.vllm_base_url) == str(worker.memory_llm_base_url)
    assert gateway.vllm_model == worker.memory_llm_model == "/models/Qwen3_14B"
    for job in ("memory-init", "memory-validate"):
        values = environment(resources(TEMPLATES / f"jobs/{job}.yaml")[0], bindings)
        settings = WorkerSettings(_env_file=None, **{k.lower(): v for k, v in values.items()})
        assert (settings.memory_admin_database_url is not None) == (job == "memory-init")


def test_every_pod_is_limited_to_the_three_target_nodes():
    expected = {f"hkh9104-10-221-248-{suffix}" for suffix in (16, 17, 18)}
    for path in TEMPLATES.rglob("*.yaml"):
        for resource in resources(path):
            if resource["kind"] not in {"Deployment", "StatefulSet", "Job", "Pod"}:
                continue
            spec = pod_spec(resource)
            affinity = spec["affinity"]["nodeAffinity"]
            terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
            # Kubernetes matchFields In accepts one node name per term; terms are ORed.
            assert all(len(term["matchFields"][0]["values"]) == 1 for term in terms)
            assert {term["matchFields"][0]["values"][0] for term in terms} == expected
            assert spec["nodeSelector"]["project"] == "lhvtt"
            assert spec["nodeSelector"]["kubernetes.io/arch"] == "amd64"
            assert {
                "key": "project",
                "operator": "Equal",
                "value": "lhvtt",
                "effect": "NoSchedule",
            } in spec["tolerations"]
            assert spec["automountServiceAccountToken"] is False


@pytest.mark.parametrize("state,success", [("1||", True), ("|1|True", False), ("||True", False)])
def test_apply_stops_on_failed_jobs_without_starting_runtime(tmp_path, state, success):
    shell = shutil.which("sh")
    if shell is None:
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        shell = str(candidate) if candidate.exists() else None
    if shell is None:
        pytest.skip("A POSIX shell is required to execute the deployment failure gate")
    release = tmp_path / "release"
    render_k8s.render(IMAGES, release)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "kubectl.log"

    def shell_path(path):
        value = path.as_posix()
        return f"/{value[0].lower()}{value[2:]}" if path.drive else value

    (fake_bin / "kubectl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$KIRA_TEST_LOG"\n'
        'case "$*" in *"get job"*) printf "%s" "$KIRA_TEST_STATE";; esac\n',
        encoding="utf-8",
        newline="\n",
    )
    (fake_bin / "kubectl").chmod(0o755)
    runner = tmp_path / "run.sh"
    # Git Bash PATH needs /c/...: C:/... would split on the drive's colon.
    runner.write_text(
        f'export PATH="{shell_path(fake_bin)}:/usr/bin:/bin"\n'
        f'export KIRA_TEST_LOG="{shell_path(log)}"\n'
        f'exec sh "{shell_path(release / "apply.sh")}" apply\n',
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        [shell, str(runner)],
        env={**os.environ, "KIRA_TEST_STATE": state},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert (result.returncode == 0) == success, result.stderr
    calls = log.read_text()
    assert ("runtime.yaml" in calls) == success
    if success:
        assert calls.index("jobs/migrate.yaml") < calls.index("jobs/memory-init.yaml")
        assert calls.index("jobs/memory-init.yaml") < calls.index("jobs/memory-validate.yaml")
        assert calls.index("jobs/memory-validate.yaml") < calls.index("runtime.yaml")
    else:
        assert "failed" in result.stderr
        assert "jobs/memory-init.yaml" not in calls
