"""Report resource and database connection budgets from the reviewable Kubernetes YAML."""

import argparse
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy/k8s"
CORE_FILES = (
    "config.yaml",
    "runtime.yaml",
    "postgres.yaml",
    "postgres-pvc.yaml",
    "jobs/migrate.yaml",
    "jobs/memory-init.yaml",
    "jobs/memory-validate.yaml",
    "optional/operator.yaml",
)
OBSERVABILITY_FILES = (
    "observability/config.yaml",
    "observability/langfuse.yaml",
    "observability/datastores.yaml",
    "observability/collector.yaml",
    "observability/s3-init.yaml",
)
RESOURCE_KEYS = ("cpu", "memory", "ephemeral-storage")
GIB = Decimal(1024**3)
SUFFIXES = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    **{suffix: Decimal(1000) ** power for power, suffix in enumerate("kMGTPE", 1)},
    **{
        suffix: Decimal(1024) ** power
        for power, suffix in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), 1)
    },
}


def quantity(value: str | int | float) -> Decimal:
    """Normalize Kubernetes decimal/binary quantities without floating point summation."""
    match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)", str(value))
    if match is None or match[2] not in SUFFIXES:
        raise ValueError("Unsupported or negative Kubernetes resource quantity")
    return Decimal(match[1]) * SUFFIXES[match[2]]


def documents(source: Path, mode: str) -> list[dict]:
    """Read only public, allowlisted templates; never discover private Secret files."""
    if mode not in {"none", "shared", "standalone"}:
        raise ValueError("Observability mode must be none, shared or standalone")
    names = list(CORE_FILES)
    if mode != "none":
        names.extend(OBSERVABILITY_FILES)
    if mode == "standalone":
        names.append("observability/metrics.yaml")
    result = []
    for name in names:
        result.extend(
            item for item in yaml.safe_load_all((source / name).read_text(encoding="utf-8")) if item
        )
    return result


def pod_spec(resource: dict) -> dict:
    return resource["spec"] if resource["kind"] == "Pod" else resource["spec"]["template"]["spec"]


def pod_resources(spec: dict, bound: str) -> dict[str, Decimal]:
    """Use the greater of regular-container sum and largest sequential init container."""
    totals = dict.fromkeys(RESOURCE_KEYS, Decimal(0))
    for container in spec["containers"]:
        for key in RESOURCE_KEYS:
            totals[key] += quantity(container["resources"][bound][key])
    for container in spec.get("initContainers", []):
        if container.get("restartPolicy") == "Always":
            raise ValueError("Restartable init containers need a separate sidecar capacity model")
        for key in RESOURCE_KEYS:
            totals[key] = max(totals[key], quantity(container["resources"][bound][key]))
    return totals


def surge_replicas(resource: dict) -> int:
    if resource["kind"] != "Deployment":
        return 0
    strategy = resource["spec"].get("strategy", {})
    if strategy.get("type", "RollingUpdate") == "Recreate":
        return 0
    value = strategy.get("rollingUpdate", {}).get("maxSurge", "25%")
    replicas = resource["spec"].get("replicas", 1)
    return (
        math.ceil(replicas * int(value[:-1]) / 100)
        if isinstance(value, str) and value.endswith("%")
        else int(value)
    )


def display_resources(values: dict[str, Decimal]) -> dict[str, float]:
    return {
        "cpu_cores": float(values["cpu"]),
        "memory_gib": float(values["memory"] / GIB),
        "ephemeral_storage_gib": float(values["ephemeral-storage"] / GIB),
    }


def total_resources(workloads: list[dict], phase: str) -> dict:
    totals = {bound: dict.fromkeys(RESOURCE_KEYS, Decimal(0)) for bound in ("requests", "limits")}
    pods = 0
    for workload in workloads:
        count = workload["replicas"] + (workload["surge_replicas"] if phase == "rollout" else 0)
        pods += count
        for bound in totals:
            for key in RESOURCE_KEYS:
                totals[bound][key] += workload["resources"][bound][key] * count
    return {"pods": pods, **{bound: display_resources(values) for bound, values in totals.items()}}


def environment(resource: dict, configs: dict[str, dict]) -> dict[str, str]:
    result = {}
    for container in pod_spec(resource)["containers"]:
        for item in container.get("envFrom", []):
            if "configMapRef" in item:
                result.update(configs[item["configMapRef"]["name"]])
        result.update(
            {item["name"]: item["value"] for item in container.get("env", []) if "value" in item}
        )
    return result


def connection_budget(postgres: dict, steady: int, rollout: int) -> dict:
    args = pod_spec(postgres)["containers"][0]["args"]
    settings = dict(value.split("=", 1) for value in args if "=" in value)
    maximum = int(settings["max_connections"])
    reserved = int(settings.get("superuser_reserved_connections", "3"))
    return {
        "steady_connections": steady,
        "rollout_connections": rollout,
        "max_connections": maximum,
        "superuser_reserved_connections": reserved,
        "remaining_non_superuser_connections": maximum - reserved - rollout,
    }


def build_report(source: Path = TEMPLATES, mode: str = "none", longhorn_copies: int = 2) -> dict:
    if longhorn_copies < 1:
        raise ValueError("Longhorn data copies must be positive")
    docs = documents(source, mode)
    resources = {
        item["metadata"]["name"]: item
        for item in docs
        if item["kind"] in {"Deployment", "StatefulSet"}
    }
    configs = {
        item["metadata"]["name"]: item["data"] for item in docs if item["kind"] == "ConfigMap"
    }
    workloads, transient = [], []
    for item in docs:
        if item["kind"] not in {"Deployment", "StatefulSet", "Job", "Pod"}:
            continue
        row = {
            "name": item["metadata"]["name"],
            "kind": item["kind"],
            "replicas": item["spec"].get("replicas", item["spec"].get("parallelism", 1)),
            "surge_replicas": surge_replicas(item),
            "resources": {
                bound: pod_resources(pod_spec(item), bound) for bound in ("requests", "limits")
            },
        }
        (transient if item["kind"] in {"Job", "Pod"} else workloads).append(row)
    claims = [item for item in docs if item["kind"] == "PersistentVolumeClaim"]
    logical = sum(
        (quantity(item["spec"]["resources"]["requests"]["storage"]) for item in claims), Decimal(0)
    )
    app_steady = app_rollout = 0
    pools = {}
    for name in ("gateway", "worker"):
        values = environment(resources[name], configs)
        async_pool = int(values["POSTGRES_POOL_SIZE"]) + int(values["POSTGRES_MAX_OVERFLOW"])
        memory_pool = int(values["MEMORY_POSTGRES_MAX_CONNECTIONS"])
        # AsyncMemory lazily creates an entity store with a second independent PgVector pool.
        per_pod = async_pool + 2 * memory_pool
        count = resources[name]["spec"].get("replicas", 1)
        app_steady += count * per_pod
        app_rollout += (count + surge_replicas(resources[name])) * per_pod
        pools[name] = {
            "async_pool": async_pool,
            "memory_pool_per_store": memory_pool,
            "memory_stores": 2,
            "per_pod": per_pod,
        }
    budgets = {
        "application": {
            **connection_budget(resources["postgres"], app_steady, app_rollout),
            "pools": pools,
        }
    }
    if mode != "none":
        # Parse only the public schema example, without reporting URL credentials.
        example = yaml.safe_load(
            (source / "observability/secrets.example.yaml").read_text(encoding="utf-8")
        )
        query = parse_qs(urlsplit(example["stringData"]["LF_DATABASE_URL"]).query)
        limit, timeout = int(query["connection_limit"][0]), int(query["pool_timeout"][0])
        count = sum(
            resources[name]["spec"].get("replicas", 1)
            for name in ("langfuse-web", "langfuse-worker")
        )
        peak = count + sum(
            surge_replicas(resources[name]) for name in ("langfuse-web", "langfuse-worker")
        )
        budgets["langfuse"] = {
            **connection_budget(resources["langfuse-postgres"], count * limit, peak * limit),
            "pool_limit": limit,
            "pool_timeout_seconds": timeout,
            "steady_pools": count,
            "rollout_pools": peak,
        }
    worker_values = environment(resources["worker"], configs)
    report = {
        "observability_mode": mode,
        "steady": total_resources(workloads, "steady"),
        "rollout": total_resources(workloads, "rollout"),
        "workloads": [
            {
                **row,
                "resources": {
                    bound: display_resources(values) for bound, values in row["resources"].items()
                },
            }
            for row in workloads
        ],
        "transient_workloads": [
            {
                **row,
                "resources": {
                    bound: display_resources(values) for bound, values in row["resources"].items()
                },
            }
            for row in transient
        ],
        "persistent_storage": {
            "claims": [
                {
                    "name": item["metadata"]["name"],
                    "gib": float(quantity(item["spec"]["resources"]["requests"]["storage"]) / GIB),
                }
                for item in claims
            ],
            "logical_gib": float(logical / GIB),
            "longhorn_data_copies": longhorn_copies,
            "replicated_data_gib": float(logical * longhorn_copies / GIB),
        },
        "connection_budgets": budgets,
        "worker_jobs": {
            "per_pod": int(worker_values["MEMORY_JOB_CONCURRENCY"]),
            "steady": int(worker_values["MEMORY_JOB_CONCURRENCY"])
            * resources["worker"]["spec"]["replicas"],
            "rollout": int(worker_values["MEMORY_JOB_CONCURRENCY"])
            * (resources["worker"]["spec"]["replicas"] + surge_replicas(resources["worker"])),
        },
        "assumptions": [
            "Rollout adds every RollingUpdate Deployment's maxSurge concurrently; "
            "terminating Pods can temporarily add further usage.",
            "Jobs run in ordered bootstrap phases, and the optional operator is separate "
            "from steady/rollout totals.",
            "Connection estimates assume one application process and one Prisma pool per Pod; "
            "both memory stores initialized.",
            "Langfuse pool limits describe the public Secret template; preserve its URL query "
            "when generating private credentials.",
            "Longhorn copy count is supplied, not a cluster verification; replicated data "
            "excludes snapshots, metadata and free-space headroom.",
            "PVC capacity is separate from node ephemeral storage; workload requests exclude "
            "image cache and existing cluster usage.",
            "Datastores remain single-primary services; multiple storage copies do not provide "
            "database failover.",
        ],
    }
    return report


def markdown(report: dict) -> str:
    lines = [
        f"Kubernetes capacity: {report['observability_mode']}",
        "",
        "| Phase | Pods | Requested CPU | Requested GiB | CPU limits | Memory limits GiB | "
        "Ephemeral request/limit GiB |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in ("steady", "rollout"):
        total = report[phase]
        request, limit = total["requests"], total["limits"]
        lines.append(
            f"| {phase} | {total['pods']} | {request['cpu_cores']:g} | "
            f"{request['memory_gib']:g} | {limit['cpu_cores']:g} | {limit['memory_gib']:g} | "
            f"{request['ephemeral_storage_gib']:g} / {limit['ephemeral_storage_gib']:g} |"
        )
    lines.extend(
        [
            "",
            "| Database | Connections steady/rollout | Maximum | Reserved for superuser | "
            "Remaining after rollout |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, budget in report["connection_budgets"].items():
        lines.append(
            f"| {name} | {budget['steady_connections']} / {budget['rollout_connections']} | "
            f"{budget['max_connections']} | {budget['superuser_reserved_connections']} | "
            f"{budget['remaining_non_superuser_connections']} |"
        )
    storage = report["persistent_storage"]
    jobs = report["worker_jobs"]
    lines.extend(
        [
            "",
            f"PVC allocation: {storage['logical_gib']:g} GiB; "
            f"{storage['longhorn_data_copies']} Longhorn data copies: "
            f"{storage['replicated_data_gib']:g} GiB before overhead.",
            f"Worker concurrency: {jobs['per_pod']} per Pod; "
            f"{jobs['steady']} steady / {jobs['rollout']} during rollout.",
            "",
            "Transient workloads (excluded above):",
            "",
            "| Workload | Pods | Requested CPU/GiB | CPU/memory limits GiB |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in report["transient_workloads"]:
        request, limit = row["resources"]["requests"], row["resources"]["limits"]
        lines.append(
            f"| {row['name']} | {row['replicas']} | {request['cpu_cores']:g} / "
            f"{request['memory_gib']:g} | {limit['cpu_cores']:g} / {limit['memory_gib']:g} |"
        )
    lines.extend(["", *[f"- {value}" for value in report["assumptions"]]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", type=Path, default=TEMPLATES)
    parser.add_argument("--observability", choices=("none", "shared", "standalone"), default="none")
    parser.add_argument("--longhorn-copies", type=int, default=2)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    try:
        report = build_report(args.templates, args.observability, args.longhorn_copies)
        print(
            json.dumps(report, indent=2) if args.format == "json" else markdown(report),
            end="\n" if args.format == "json" else "",
        )
    except yaml.YAMLError:
        print("Capacity report failed: invalid public YAML template", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Capacity report failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
