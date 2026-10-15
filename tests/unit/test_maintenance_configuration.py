import asyncio
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sitg_bot import database_maintenance as cli
from sitg_bot.server import ConsoleApplicationServer
from sitg_bot.services.database_maintenance import DatabaseMaintenanceService, RetentionPolicy


@pytest.mark.parametrize("values", [
    {"success": timedelta(0)}, {"failure": timedelta(days=-1)},
    {"expiry_grace": timedelta(seconds=-1)},
])
def test_invalid_retention_is_rejected(values):
    with pytest.raises(ValueError):
        RetentionPolicy(**values)


@pytest.mark.parametrize("values", [
    {"batch_size": 0}, {"batch_size": 1001}, {"max_batches": 0},
    {"max_runtime": 0}, {"max_runtime": float("nan")}, {"max_runtime": float("inf")},
])
def test_invalid_work_limits_are_rejected(values):
    with pytest.raises(ValueError):
        DatabaseMaintenanceService(object(), **values)


@pytest.mark.parametrize("flags, apply", [([], False), (["--dry-run"], False), (["--apply"], True)])
def test_cli_preview_is_default(monkeypatch, flags, apply):
    main = AsyncMock()
    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(sys, "argv", ["sitg-db-maintenance", *flags])
    cli.run()
    assert main.call_args.args[0].apply is apply


def test_cli_retention_flags_override_environment(monkeypatch):
    main = AsyncMock()
    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setenv("SITG_DB_SUCCESS_RETENTION_DAYS", "9")
    monkeypatch.setenv("SITG_DB_FAILURE_RETENTION_DAYS", "45")
    monkeypatch.setattr(sys, "argv", ["sitg-db-maintenance", "--success-retention-days", "2"])
    cli.run()
    assert cli.retention_policy(main.call_args.args[0]) == RetentionPolicy(
        success=timedelta(days=2), failure=timedelta(days=45),
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_hourly_task_is_opt_in(monkeypatch, enabled):
    listener = SimpleNamespace(
        sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 8765))],
        close=Mock(), wait_closed=AsyncMock(),
    )
    monkeypatch.setattr(asyncio, "start_server", AsyncMock(return_value=listener))
    server = ConsoleApplicationServer(object(), maintenance_enabled=enabled)
    for name in ("_job_scheduler_loop", "_job_worker_loop", "_console_updates_loop",
                 "_maintenance_loop"):
        monkeypatch.setattr(server, name, AsyncMock())
    try:
        await server.start()
        await asyncio.sleep(0)
        assert server._maintenance_loop.await_count == int(enabled)
    finally:
        await server.close()


async def test_hourly_task_recovers_from_failure(monkeypatch):
    server = ConsoleApplicationServer(object())
    server.maintenance.run_once = AsyncMock(side_effect=[RuntimeError("unavailable"), None])
    intervals = []

    async def sleep(interval):
        intervals.append(interval)
        server._closing = len(intervals) == 2

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await server._maintenance_loop()
    assert intervals == [3600, 3600]
    assert server.maintenance.run_once.await_count == 2
