import importlib.util
import os
import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import ArgumentError

SUPPORT = Path(__file__).resolve().parents[1] / "integration" / "database_support.py"
spec = importlib.util.spec_from_file_location("database_creation_support", SUPPORT)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


@pytest.mark.parametrize("failure", ["missing", "invalid", "connect", "create"])
async def test_failed_creation_leaves_no_receipt_and_can_be_retried(monkeypatch, tmp_path, failure):
    record = tmp_path / "ownership.env"
    admin = AsyncMock()
    initialized = AsyncMock()
    connect = AsyncMock(return_value=admin)
    monkeypatch.setattr(support, "connect", connect)
    monkeypatch.setenv("SITG_TEST_ADMIN_URL", "postgresql://fixture:dummy@localhost/postgres")
    expected = RuntimeError
    if failure == "missing":
        monkeypatch.delenv("SITG_TEST_ADMIN_URL")
        expected = KeyError
    elif failure == "invalid":
        monkeypatch.setenv("SITG_TEST_ADMIN_URL", "not-a-url")
        expected = ArgumentError
    elif failure == "connect":
        connect.side_effect = ConnectionError("unavailable")
        expected = ConnectionError
    else:
        admin.execute.side_effect = RuntimeError("creation refused")
    opened = []
    real_open = os.open

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(support.os, "open", track_open)
    with pytest.raises(expected):
        await support.create("sitg_test_creation", record)
    assert not record.exists()
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert admin.close.await_count == int(failure == "create")

    monkeypatch.setenv("SITG_TEST_ADMIN_URL", "postgresql://fixture:dummy@localhost/postgres")
    connect.side_effect = [admin, initialized]
    admin.execute.side_effect = None
    await support.create("sitg_test_creation", record)
    exports = dict(shlex.split(line.removeprefix("export "))[0].split("=", 1)
                   for line in record.read_text().splitlines())
    assert exports["TEST_DATABASE_URL"].endswith("/sitg_test_creation")
    assert len(exports["SITG_TEST_DATABASE_TOKEN"]) == 64
    assert record.stat().st_mode & 0o777 == 0o600
    initialized.execute.assert_any_await(
        "INSERT INTO sitg_test_control.owner(token_hash) VALUES ($1)",
        support.hashlib.sha256(exports["SITG_TEST_DATABASE_TOKEN"].encode()).hexdigest(),
    )
    initialized.close.assert_awaited_once()


async def test_existing_receipt_is_preserved_and_admin_connection_closed(monkeypatch, tmp_path):
    record = tmp_path / "ownership.env"
    record.write_text("existing ownership receipt")
    admin = AsyncMock()
    monkeypatch.setenv("SITG_TEST_ADMIN_URL", "postgresql://fixture:dummy@localhost/postgres")
    monkeypatch.setattr(support, "connect", AsyncMock(return_value=admin))
    with pytest.raises(FileExistsError):
        await support.create("sitg_test_creation", record)
    assert record.read_text() == "existing ownership receipt"
    admin.execute.assert_not_awaited()
    admin.close.assert_awaited_once()
