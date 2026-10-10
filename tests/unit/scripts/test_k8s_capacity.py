"""Guard rollout capacity, database headroom and single-writer scheduling contracts."""

import json
import shutil
import subprocess
import sys
from decimal import Decimal

import pytest
import yaml

from scripts.deploy import capacity_report


@pytest.mark.parametrize(
    "mode,pods,requests,limits,rollout_pods,rollout_requests,rollout_limits,pvc",
    [
        ("none", 9, (29.5, 61.5), (78, 139), 12, (38, 74), (100, 172), 20),
        ("shared", 18, (59.5, 137.5), (170, 299), 23, (74, 162), (210, 356), 72),
        ("standalone", 20, (64.5, 146.5), (186, 327), 25, (79, 171), (226, 384), 83),
    ],
)
def test_selected_modes_count_workloads_surge_and_storage_separately(
    mode, pods, requests, limits, rollout_pods, rollout_requests, rollout_limits, pvc
):
    report = capacity_report.build_report(mode=mode)
    for phase, expected_pods, expected_request, expected_limit in (
        ("steady", pods, requests, limits),
        ("rollout", rollout_pods, rollout_requests, rollout_limits),
    ):
        total = report[phase]
        assert total["pods"] == expected_pods
        assert (total["requests"]["cpu_cores"], total["requests"]["memory_gib"]) == expected_request
        assert (total["limits"]["cpu_cores"], total["limits"]["memory_gib"]) == expected_limit
        assert total["requests"]["ephemeral_storage_gib"] == expected_pods / 8
        assert total["limits"]["ephemeral_storage_gib"] == expected_pods
    storage = report["persistent_storage"]
    assert storage["logical_gib"] == pvc
    assert storage["longhorn_data_copies"] == 2
    assert storage["replicated_data_gib"] == 2 * pvc
    names = {row["name"] for row in report["transient_workloads"]}
    assert {"migrate", "memory-init", "memory-validate", "kira-operator"} <= names
    assert ("langfuse-s3-init" in names) == (mode != "none")
    assert names.isdisjoint({row["name"] for row in report["workloads"]})


def test_all_database_pools_fit_simultaneous_rollout_with_operator_headroom():
    report = capacity_report.build_report(mode="standalone")
    app = report["connection_budgets"]["application"]
    assert app["pools"] == {
        role: {"async_pool": 7, "memory_pool_per_store": 3, "memory_stores": 2, "per_pod": 13}
        for role in ("gateway", "worker")
    }
    assert (app["steady_connections"], app["rollout_connections"]) == (65, 91)
    langfuse = report["connection_budgets"]["langfuse"]
    assert (langfuse["pool_limit"], langfuse["pool_timeout_seconds"]) == (15, 10)
    assert (langfuse["steady_pools"], langfuse["rollout_pools"]) == (4, 6)
    assert (langfuse["steady_connections"], langfuse["rollout_connections"]) == (60, 90)
    for budget in (app, langfuse):
        assert budget["max_connections"] == 200
        assert budget["superuser_reserved_connections"] == 3
        usable = budget["max_connections"] - budget["superuser_reserved_connections"]
        # Leave at least 25% for admin/bootstrap, health checks and transient connection overlap.
        assert budget["rollout_connections"] <= usable * 0.75
        assert budget["remaining_non_superuser_connections"] >= 20
    assert report["worker_jobs"] == {"per_pod": 2, "steady": 4, "rollout": 6}


def test_report_recalculates_pool_and_replica_changes_instead_of_using_a_static_table(tmp_path):
    source = tmp_path / "templates"
    shutil.copytree(capacity_report.TEMPLATES, source)
    config = list(yaml.safe_load_all((source / "config.yaml").read_text()))
    runtime_config = next(
        item for item in config if item["metadata"]["name"] == "kira-runtime-config"
    )
    runtime_config["data"]["MEMORY_POSTGRES_MAX_CONNECTIONS"] = "4"
    (source / "config.yaml").write_text(yaml.safe_dump_all(config))
    runtime = list(yaml.safe_load_all((source / "runtime.yaml").read_text()))
    worker = next(
        item
        for item in runtime
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "worker"
    )
    worker["spec"]["replicas"] = 3
    (source / "runtime.yaml").write_text(yaml.safe_dump_all(runtime))
    report = capacity_report.build_report(source)
    app = report["connection_budgets"]["application"]
    assert app["pools"]["worker"]["per_pod"] == 15
    assert (app["steady_connections"], app["rollout_connections"]) == (90, 120)
    assert report["steady"]["pods"] == 10
    assert report["worker_jobs"] == {"per_pod": 2, "steady": 6, "rollout": 8}


def test_resource_requests_and_limits_fit_surviving_nodes_without_hard_spread_deadlocks():
    docs = capacity_report.documents(capacity_report.TEMPLATES, "standalone")
    deployments = [item for item in docs if item["kind"] == "Deployment"]
    for item in docs:
        if item["kind"] not in {"Deployment", "StatefulSet", "Job", "Pod"}:
            continue
        spec = capacity_report.pod_spec(item)
        for container in spec["containers"] + spec.get("initContainers", []):
            for key in capacity_report.RESOURCE_KEYS:
                request = capacity_report.quantity(container["resources"]["requests"][key])
                limit = capacity_report.quantity(container["resources"]["limits"][key])
                assert 0 < request <= limit
        maximum = capacity_report.pod_resources(spec, "limits")
        assert maximum["cpu"] <= 96
        assert maximum["memory"] <= capacity_report.quantity("247Gi")
        assert maximum["ephemeral-storage"] <= capacity_report.quantity("47.4Gi")
    budgets = [item for item in docs if item["kind"] == "PodDisruptionBudget"]
    for item in deployments:
        if item["spec"]["replicas"] < 2:
            continue
        spec = capacity_report.pod_spec(item)
        labels = item["spec"]["template"]["metadata"]["labels"]
        matching = [
            budget
            for budget in budgets
            if budget["spec"]["selector"]["matchLabels"].items() <= labels.items()
        ]
        assert len(matching) == 1
        assert 1 <= matching[0]["spec"]["minAvailable"] <= item["spec"]["replicas"] - 1
        assert all(
            rule["whenUnsatisfiable"] == "ScheduleAnyway"
            for rule in spec["topologySpreadConstraints"]
        )
        assert "requiredDuringSchedulingIgnoredDuringExecution" not in spec["affinity"].get(
            "podAntiAffinity", {}
        )
        assert item["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    total = capacity_report.build_report(mode="standalone")["rollout"]
    # Nominal capacity of two surviving nodes; check existing cluster load separately.
    assert total["requests"]["cpu_cores"] < 2 * 96
    assert total["requests"]["memory_gib"] < 2 * 247
    assert total["limits"]["ephemeral_storage_gib"] < 2 * 47.4
    assert total["pods"] < 2 * 110


def test_collector_has_one_writer_and_shared_rwo_stores_do_not_scale_as_ha():
    docs = capacity_report.documents(capacity_report.TEMPLATES, "standalone")
    collector = next(
        item
        for item in docs
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "otel-collector"
    )
    assert collector["spec"]["replicas"] == 1
    assert collector["spec"]["strategy"] == {"type": "Recreate"}
    assert capacity_report.surge_replicas(collector) == 0
    claims = {
        item["metadata"]["name"]: item for item in docs if item["kind"] == "PersistentVolumeClaim"
    }
    for item in docs:
        if item["kind"] != "StatefulSet":
            continue
        assert item["spec"]["replicas"] == 1
        volumes = capacity_report.pod_spec(item)["volumes"]
        bound_claims = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in volumes
            if "persistentVolumeClaim" in volume
        ]
        assert len(bound_claims) == 1
        claim = claims[bound_claims[0]]
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert claim["spec"]["storageClassName"] == "longhorn-storage-2-retain"


def test_quantity_and_init_resource_math_use_scheduler_units():
    assert capacity_report.quantity("250m") == Decimal("0.25")
    assert capacity_report.quantity("1Gi") == Decimal(1024**3)
    assert capacity_report.quantity("1G") == Decimal(10**9)
    assert capacity_report.quantity("1e3") == Decimal(1000)
    containers = [
        {
            "resources": {
                "requests": {"cpu": "250m", "memory": "256Mi", "ephemeral-storage": "128Mi"}
            }
        },
        {
            "resources": {
                "requests": {"cpu": "750m", "memory": "768Mi", "ephemeral-storage": "128Mi"}
            }
        },
    ]
    init = [
        {"resources": {"requests": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "512Mi"}}}
    ]
    total = capacity_report.pod_resources(
        {"containers": containers, "initContainers": init}, "requests"
    )
    assert total == {
        "cpu": Decimal(2),
        "memory": capacity_report.quantity("1Gi"),
        "ephemeral-storage": capacity_report.quantity("512Mi"),
    }
    deployment = {
        "kind": "Deployment",
        "spec": {"replicas": 3, "strategy": {"rollingUpdate": {"maxSurge": "25%"}}},
    }
    assert capacity_report.surge_replicas(deployment) == 1
    with pytest.raises(ValueError, match="negative"):
        capacity_report.quantity("-1Gi")


def test_runtime_memory_budgets_leave_space_inside_container_limits():
    docs = capacity_report.documents(capacity_report.TEMPLATES, "standalone")
    workloads = {
        item["metadata"]["name"]: item
        for item in docs
        if item["kind"] in {"Deployment", "StatefulSet"}
    }
    for name in ("postgres", "langfuse-postgres"):
        container = capacity_report.pod_spec(workloads[name])["containers"][0]
        settings = dict(arg.split("=", 1) for arg in container["args"] if "=" in arg)
        buffer_bytes = capacity_report.quantity(settings["shared_buffers"].replace("GB", "Gi"))
        memory_request = capacity_report.quantity(container["resources"]["requests"]["memory"])
        assert buffer_bytes <= memory_request / 4
    for name in ("langfuse-web", "langfuse-worker"):
        container = capacity_report.pod_spec(workloads[name])["containers"][0]
        options = next(env["value"] for env in container["env"] if env["name"] == "NODE_OPTIONS")
        heap_bytes = capacity_report.quantity(options.split("=")[1] + "Mi")
        memory_limit = capacity_report.quantity(container["resources"]["limits"]["memory"])
        assert heap_bytes <= memory_limit * Decimal("0.75")
    config = next(item for item in docs if item["metadata"]["name"] == "kira-otel-config")
    limiter = yaml.safe_load(config["data"]["collector.yaml"])["processors"]["memory_limiter"]
    container = capacity_report.pod_spec(workloads["otel-collector"])["containers"][0]
    go_limit = next(env["value"] for env in container["env"] if env["name"] == "GOMEMLIMIT")
    go_bytes = capacity_report.quantity(go_limit.removesuffix("B"))
    hard_bytes = capacity_report.quantity(str(limiter["limit_mib"]) + "Mi")
    pod_limit = capacity_report.quantity(container["resources"]["limits"]["memory"])
    assert 0 < limiter["spike_limit_mib"] < limiter["limit_mib"]
    assert go_bytes <= hard_bytes * Decimal("0.8")
    assert hard_bytes <= pod_limit * Decimal("0.8")


def test_report_cli_outputs_reviewable_json_without_discovering_or_exposing_secrets(tmp_path):
    source = tmp_path / "templates"
    shutil.copytree(capacity_report.TEMPLATES, source)
    (source / "private.yaml").write_text("invalid_yaml: [ synthetic-private-canary")
    example_path = source / "observability/secrets.example.yaml"
    example = yaml.safe_load(example_path.read_text())
    example["stringData"]["LF_DATABASE_URL"] = (
        "postgresql://synthetic-user:synthetic-secret-canary@langfuse-postgres:5432/langfuse?connection_limit=15&pool_timeout=10"
    )
    example_path.write_text(yaml.safe_dump(example))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.deploy.capacity_report",
            "--templates",
            str(source),
            "--observability",
            "standalone",
            "--format",
            "json",
        ],
        cwd=capacity_report.ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "synthetic-private-canary" not in result.stdout + result.stderr
    assert "synthetic-secret-canary" not in result.stdout + result.stderr
    assert "synthetic-user" not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["rollout"]["pods"] == 25
    rendered = capacity_report.markdown(report)
    assert "64.5" in rendered and "171" in rendered
    assert "65 / 91" in rendered and "60 / 90" in rendered
    assert "166 GiB before overhead" in rendered
    assert "terminating Pods" in rendered
