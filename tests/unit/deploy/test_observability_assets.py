import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
OBSERVABILITY = ROOT / "deploy" / "observability"


def test_prometheus_scrapes_only_collector_metric_endpoints() -> None:
    config = (OBSERVABILITY / "prometheus.yaml").read_text(encoding="utf-8")

    assert "otel-collector:8889" in config
    assert "otel-collector:8888" in config
    assert "gateway:" not in config
    assert "worker:" not in config

    datasource = (
        OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
    ).read_text(encoding="utf-8")
    assert "manageAlerts: true" in datasource


def test_queue_dashboard_uses_max_not_sum_across_worker_replicas() -> None:
    dashboard_path = OBSERVABILITY / "grafana" / "dashboards" / "kira-overview.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    expressions = [
        target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
    ]
    queue_expressions = [expr for expr in expressions if "kira_memory_job_queue_depth" in expr]

    assert queue_expressions == ["max by (status) (kira_memory_job_queue_depth)"]
    assert all("sum" not in expr for expr in queue_expressions)


def test_dashboard_and_alerts_use_verified_collector_export_names() -> None:
    dashboard = (OBSERVABILITY / "grafana" / "dashboards" / "kira-overview.json").read_text(
        encoding="utf-8"
    )
    alerts = (OBSERVABILITY / "prometheus-rules.yaml").read_text(encoding="utf-8")

    for exported_name in (
        "kira_chat_request_duration_seconds",
        "kira_memory_job_process_count_total",
        "kira_memory_job_queue_depth",
        "kira_memory_job_queue_database_available_ratio",
        "otelcol_receiver_refused_metric_points",
    ):
        assert exported_name in dashboard or exported_name in alerts

    assert "kira_memory_job_processing_total" not in dashboard + alerts
    assert "kira_context_rewrite_total" not in dashboard + alerts


def test_metrics_ui_is_opt_in_for_local_compose() -> None:
    compose = (ROOT / "compose.observability.yaml").read_text(encoding="utf-8")

    assert compose.count('profiles: ["metrics"]') == 2


def test_langfuse_acceptance_uses_only_required_local_dependencies() -> None:
    compose = (ROOT / "compose.langfuse.yaml").read_text(encoding="utf-8")
    collector = (OBSERVABILITY / "collector-langfuse-local.yaml").read_text(encoding="utf-8")

    for service in (
        "langfuse-web:",
        "langfuse-worker:",
        "langfuse-postgres:",
        "langfuse-clickhouse:",
        "langfuse-redis:",
        "langfuse-minio:",
    ):
        assert service in compose
    assert "prometheus:" not in compose
    assert "grafana:" not in compose
    assert "loki" not in compose.lower()
    assert "otlphttp/langfuse" in collector
    assert "x-langfuse-ingestion-version" in collector
    assert "sending_queue" in collector
    assert "filter/drop_idle_memory_job_claims" in collector
    assert 'name == "memory_job.claim"' in collector
