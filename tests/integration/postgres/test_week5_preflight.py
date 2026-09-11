"""Optional real-PostgreSQL gate for the read-only Week 5 preflight."""

import asyncio
import os
import sys

import pytest

from evaluation.config import EvalConfig
from evaluation.models import Probe
from evaluation.postgres import probe_database

pytestmark = pytest.mark.postgres_integration


def test_week5_pgvector_connection_without_writes():
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    config = EvalConfig(memory_database_url=url)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        assert runner.run(probe_database(config, Probe.PGVECTOR)) == {}
