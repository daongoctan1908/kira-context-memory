"""Suite-scoped preflight orchestration. No benchmark, formation or persistent writes."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from evaluation.config import EvalConfig
from evaluation.errors import PreflightError
from evaluation.mock import mock_database, mock_response
from evaluation.models import (
    Outcome,
    PreflightReport,
    Probe,
    ProbeResult,
    Profile,
    Reason,
    Suite,
    SuiteReadiness,
)
from evaluation.postgres import probe_database
from evaluation.providers import ProviderProbes

DatabaseProbe = Callable[[EvalConfig, Probe, int | None], Awaitable[dict]]


def required_probes(suite: Suite, formation_mode: str) -> tuple[Probe, ...]:
    formation = (Probe.EXTRACTION_JSON,)
    retrieval = (Probe.EMBEDDING_BATCH, Probe.PGVECTOR, Probe.MEMORY_SCHEMA)
    persistent = (*formation, *retrieval, Probe.CONVERSATION_DB)
    if suite == Suite.FORMATION:
        return persistent if formation_mode == "persistent" else formation
    if suite == Suite.RETRIEVAL:
        return retrieval
    if suite == Suite.REWRITE:
        return (Probe.REWRITE_CHAT,)
    return (*persistent, Probe.REWRITE_CHAT, Probe.GATEWAY, Probe.WORKER, Probe.KIRA)


def readiness(results: tuple[ProbeResult, ...]) -> Outcome:
    for outcome in (Outcome.PROTOCOL_ERROR, Outcome.DEPENDENCY_ERROR, Outcome.NOT_RUN):
        if any(result.outcome == outcome for result in results):
            return outcome
    return Outcome.PASS


async def run_preflight(
    config: EvalConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    database_probe: DatabaseProbe | None = None,
) -> PreflightReport:
    started_at = datetime.now(UTC)
    simulated = config.profile == Profile.MOCK
    if simulated:
        transport = transport or httpx.MockTransport(mock_response)
        database_probe = database_probe or mock_database
    database_probe = database_probe or probe_database
    requirements = {suite: required_probes(suite, config.formation_mode) for suite in config.suites}
    needed = {probe for probes in requirements.values() for probe in probes}
    results: dict[Probe, ProbeResult] = {}
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        providers = ProviderProbes(client, config)
        for probe in Probe:
            if probe not in needed:
                continue
            operation: Callable[[], Awaitable[dict]] | None = None
            reason = Reason.MISSING_CONFIG
            match probe:
                case Probe.EXTRACTION_JSON if config.extraction.configured:

                    async def operation() -> dict:
                        return await providers.chat(extraction=True)
                case Probe.REWRITE_CHAT if config.rewrite.configured:

                    async def operation() -> dict:
                        return await providers.chat(extraction=False)
                case Probe.EMBEDDING_BATCH if config.embedding.configured:
                    operation = providers.embeddings
                case Probe.PGVECTOR | Probe.MEMORY_SCHEMA | Probe.CONVERSATION_DB:
                    configured = (
                        config.database_url
                        if probe == Probe.CONVERSATION_DB
                        else (config.memory_database_url)
                    )
                    embedding_result = results.get(Probe.EMBEDDING_BATCH)
                    dimension = embedding_result.embedding_dimension if embedding_result else None
                    if configured:
                        if probe == Probe.MEMORY_SCHEMA and not dimension:
                            reason = Reason.PREREQUISITE_FAILED
                        else:

                            async def operation(probe: Probe = probe, dims=dimension) -> dict:
                                return await database_probe(config, probe, dims)
                case Probe.GATEWAY if config.gateway_url:

                    async def operation() -> dict:
                        return await providers.health(str(config.gateway_url))
                case Probe.WORKER if config.worker_url:

                    async def operation() -> dict:
                        return await providers.health(str(config.worker_url))
                case Probe.KIRA if config.kira_mock_url:

                    async def operation() -> dict:
                        return await providers.kira_mock(str(config.kira_mock_url))

            if operation is None:
                results[probe] = ProbeResult(
                    probe=probe,
                    outcome=Outcome.NOT_RUN,
                    reason=reason,
                    simulated=simulated or probe == Probe.KIRA,
                )
                continue
            started = time.perf_counter()
            try:
                async with asyncio.timeout(config.total_timeout_seconds):
                    details = await operation()
                result = ProbeResult(probe=probe, outcome=Outcome.PASS, attempted=True, **details)
            except PreflightError as error:
                result = ProbeResult(
                    probe=probe,
                    outcome=error.outcome,
                    reason=error.reason,
                    http_status=error.http_status,
                    attempted=True,
                )
            except TimeoutError:
                result = ProbeResult(
                    probe=probe,
                    outcome=Outcome.DEPENDENCY_ERROR,
                    reason=Reason.TIMEOUT,
                    attempted=True,
                )
            results[probe] = result.model_copy(
                update={
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "simulated": simulated or probe == Probe.KIRA,
                }
            )
    return PreflightReport(
        run_id=uuid4(),
        started_at=started_at,
        profile=config.profile,
        simulated=simulated,
        config_sha256=config.fingerprint(),
        configuration=config.model_dump(mode="json"),
        checks=tuple(results.values()),
        suites=tuple(
            SuiteReadiness(
                suite=suite,
                required_probes=probes,
                outcome=readiness(tuple(results[probe] for probe in probes)),
            )
            for suite, probes in requirements.items()
        ),
    )
