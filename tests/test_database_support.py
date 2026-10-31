"""Real two-process lease/cleanup regression; run outside the leased pytest suite."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tests/integration/database_support.py"
pytestmark = pytest.mark.integration


def wait_for(path, child):
    deadline = time.monotonic() + 15
    while not path.exists():
        assert child.poll() is None, "Consumer exited before its readiness receipt"
        assert time.monotonic() < deadline, "Timed out waiting for consumer"
        time.sleep(0.05)


def test_two_consumers_serialize_and_cleanup_refuses_active_use(tmp_path):
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is not configured")
    assert not os.environ.get("SITG_TEST_DATABASE_LEASE"), "Run this test outside a DB lease"
    table = "lease_" + uuid4().hex
    ready, release, second = [tmp_path / name for name in ("ready", "release", "second")]
    command = [sys.executable, str(SUPPORT), "run", "--", sys.executable, str(__file__)]
    first = subprocess.Popen([*command, "hold", table, str(ready), str(release)])
    other = None
    try:
        wait_for(ready, first)
        cleanup = subprocess.run(
            [sys.executable, str(SUPPORT), "cleanup"], capture_output=True, timeout=10,
        )
        assert cleanup.returncode != 0
        assert b"active consumer" in cleanup.stderr
        other = subprocess.Popen([*command, "reuse", table, str(second)])
        # The second consumer must not enter while the first still owns its data.
        time.sleep(0.25)
        assert other.poll() is None and not second.exists()
        release.touch()
        assert first.wait(timeout=15) == 0
        assert other.wait(timeout=15) == 0
        assert second.read_text() == "retained"
        # A bad token cannot run a consumer (or reset/drop this database).
        wrong = subprocess.run(
            [*command, "reuse", table, str(second)],
            env={**os.environ, "SITG_TEST_DATABASE_TOKEN": "bad-token" * 8},
            capture_output=True, timeout=10,
        )
        assert wrong.returncode != 0 and b"ownership mismatch" in wrong.stderr
    finally:
        release.touch()
        for child in (first, other):
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=15)


async def consumer():
    # The subprocess is a real lease consumer, not a simulated lock/connection.
    sys.path.insert(0, str(SUPPORT.parent))
    from database_support import connect, target

    action, table, receipt, *extra = sys.argv[1:]
    url, _ = target()
    conn = await connect(url)
    try:
        if action == "hold":
            await conn.execute(f'CREATE TABLE "{table}" (value text)')
            await conn.execute(f'INSERT INTO "{table}" VALUES (\'retained\')')
            Path(receipt).touch()
            deadline = time.monotonic() + 20
            while not Path(extra[0]).exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            assert await conn.fetchval(f'SELECT value FROM "{table}"') == "retained"
        else:
            value = await conn.fetchval(f'SELECT value FROM "{table}"')
            Path(receipt).write_text(value)
            await conn.execute(f'DROP TABLE "{table}"')
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(consumer())
