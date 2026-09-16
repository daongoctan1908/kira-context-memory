# Phase 5 metric contract

Phase 5 dual-writes the existing process-local Prometheus metrics and their reviewed OTel
equivalents. Prometheus and Grafana scrape only the Collector OTel endpoints. The legacy Gateway
and Worker `/metrics` endpoints remain available for parity comparison until the Phase 7 cutover;
they are not a data source for the new dashboards or alerts.

Alert rules are evaluated once by Prometheus and exposed read-only in Grafana through the
provisioned data source. They are not duplicated as Grafana-managed rules.

## Retained metric mapping

The last column is the metric name verified against the Collector `0.160.0` Prometheus exporter.

| Legacy name | OTel instrument | Collector export |
| --- | --- | --- |
| `kira_context_recent_messages` | `kira.context.recent_messages` | `kira_context_recent_messages` |
| `kira_context_estimated_recent_tokens` | `kira.context.estimated_recent_tokens` | `kira_context_estimated_recent_tokens` |
| `kira_memory_search_total` | `kira.memory.search.count` | `kira_memory_search_count_total` |
| `kira_memory_search_duration_seconds` | `kira.memory.search.duration` (`s`) | `kira_memory_search_duration_seconds` |
| `kira_memory_search_results` | `kira.memory.search.result_count` | `kira_memory_search_result_count` |
| `kira_memory_job_schedule_total` | `kira.memory.job.schedule.count` | `kira_memory_job_schedule_count_total` |
| `kira_context_rewrite_total` | `kira.context.rewrite.count` | `kira_context_rewrite_count_total` |
| `kira_context_rewrite_duration_seconds` | `kira.context.rewrite.duration` (`s`) | `kira_context_rewrite_duration_seconds` |
| `kira_context_degraded_total` | `kira.context.degraded.count` | `kira_context_degraded_count_total` |
| `kira_conversation_write_total` | `kira.conversation.write.count` | `kira_conversation_write_count_total` |
| `kira_memory_job_queue_depth` | `kira.memory.job.queue.depth` | `kira_memory_job_queue_depth` |
| `kira_memory_job_oldest_pending_age_seconds` | `kira.memory.job.oldest_pending.age` (`s`) | `kira_memory_job_oldest_pending_age_seconds` |
| `kira_memory_job_claim_total` | `kira.memory.job.claim.count` | `kira_memory_job_claim_count_total` |
| `kira_memory_job_processing_total` | `kira.memory.job.process.count` | `kira_memory_job_process_count_total` |
| `kira_memory_job_processing_duration_seconds` | `kira.memory.job.process.duration` (`s`) | `kira_memory_job_process_duration_seconds` |
| `kira_memory_job_attempt_count` | `kira.memory.job.attempt.number` | `kira_memory_job_attempt_number` |
| `kira_memory_job_lifecycle_event_count` | `kira.memory.lifecycle_event.count` | `kira_memory_lifecycle_event_count` |
| `kira_memory_job_cleanup_total` | `kira.memory.job.cleanup.count` | `kira_memory_job_cleanup_count_total` |
| `kira_memory_worker_runner_active` | `kira.memory.worker.runner.active` (`1`) | `kira_memory_worker_runner_active_ratio` |
| `kira_memory_job_queue_database_available` | `kira.memory.job.queue.database.available` (`1`) | `kira_memory_job_queue_database_available_ratio` |
| `kira_memory_job_in_flight` | `kira.memory.job.in_flight` | `kira_memory_job_in_flight` |
| `kira_memory_job_database_backoff_seconds` | `kira.memory.job.database_backoff` (`s`) | `kira_memory_job_database_backoff_seconds` |

## Phase 5 additions

| Purpose | OTel instrument | Collector export |
| --- | --- | --- |
| complete `/chat`, through SSE completion | `kira.chat.request.duration` (`s`) | `kira_chat_request_duration_seconds` |
| Gateway, Worker and Mem0 stage latency | `kira.stage.duration` (`s`) | `kira_stage_duration_seconds` |
| KiRa stream lifetime | `kira.stream.duration` (`s`) | `kira_stream_duration_seconds` |
| KiRa first event | `kira.stream.first_event.duration` (`s`) | `kira_stream_first_event_duration_seconds` |
| KiRa first content | `kira.stream.first_content.duration` (`s`) | `kira_stream_first_content_duration_seconds` |
| durable enqueue-to-claim wait | `kira.memory.job.queue_wait.duration` (`s`) | `kira_memory_job_queue_wait_duration_seconds` |
| Collector rejected points | Collector self-metric | `otelcol_receiver_refused_metric_points` |
| Collector failed points | Collector self-metric | `otelcol_receiver_failed_metric_points` |

The generic stage histogram supplies recent-read, context-build, persistence, and internal Mem0
formation-stage latency with a bounded `stage` attribute. It does not contain IDs or content.

## Intentional semantic changes

- The legacy Worker processing label remains `outcome="success"` during dual-read. The OTel
  equivalent is `outcome="completed"`, matching the durable job state.
- Duration instruments use canonical OTel unit `s`; the Collector appends `_seconds`.
- Boolean observable gauges use unit `1`; the Collector appends `_ratio`.
- OTel Worker gauges observe only a lock-protected in-process snapshot. The queue sampler and
  readiness calculation remain outside the OTel callbacks.
- Queue depth and oldest-pending age describe one shared PostgreSQL queue. Queries across Worker
  replicas must use `max`, never `sum`.

Metric attributes are limited to `stage`, `operation`, `outcome`, `dependency`, `status`, and
`kind`, each with fixed enum values. Request, trace, span, user, session, conversation, turn,
boundary, provider-request, memory, and event IDs are forbidden metric dimensions.
