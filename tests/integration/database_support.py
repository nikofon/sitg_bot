"""One explicitly disposable PostgreSQL database, leased across test consumers.

Use the CLI from the operations runbook, never point it at an application DB.
The private environment record survives workers; fixtures reset only public,
under a live whole-command lease. No fixture creates or drops a database.
"""

import argparse
import asyncio
import hashlib
import os
import re
import secrets
import shlex
import signal
import sys
from contextlib import suppress
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

LOCK = 734916028174


def target():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if not re.fullmatch(r"sitg_test_[a-z0-9_]+", url.database or ""):
        raise RuntimeError("Refusing database outside the sitg_test_ disposable namespace")
    token = os.environ["SITG_TEST_DATABASE_TOKEN"]
    if len(token) < 32:
        raise RuntimeError("Missing disposable database ownership token")
    return url, hashlib.sha256(token.encode()).hexdigest()


async def connect(url):
    return await asyncpg.connect(url.set(drivername="postgresql").render_as_string(
        hide_password=False
    ))


async def owned(conn, digest, *, allow_closing=False):
    stored = await conn.fetchval("SELECT token_hash FROM sitg_test_control.owner")
    if stored != digest:
        raise RuntimeError("Disposable database ownership mismatch")
    if not allow_closing and await conn.fetchval("SELECT closing FROM sitg_test_control.owner"):
        raise RuntimeError("Disposable database is closing; do not start another consumer")


async def reset_database():
    """Reset between tests only when the CLI's live lease authorizes this process."""
    url, digest = target()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        raise RuntimeError("Shared test database requires serial pytest, not pytest-xdist")
    pid = int(os.environ["SITG_TEST_DATABASE_LEASE"])
    conn = await connect(url)
    try:
        await owned(conn, digest)
        held = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = $1 AND granted AND classid = $2 AND objid = $3 "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))",
            pid, LOCK >> 32, LOCK & 0xFFFFFFFF,
        )
        if not held:
            raise RuntimeError(
                "No live test database lease; run via tests/integration/database_support.py run"
            )
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


async def create(name, record):
    if not re.fullmatch(r"sitg_test_[a-z0-9_]+", name):
        raise RuntimeError("Database name must start sitg_test_ and contain only a-z, 0-9, _")
    admin = make_url(os.environ["SITG_TEST_ADMIN_URL"])
    token = secrets.token_hex(32)
    url = admin.set(database=name, drivername="postgresql+asyncpg")
    conn = await connect(admin)
    try:
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        except BaseException:
            os.close(fd)
            Path(record).unlink()
            raise
    finally:
        await conn.close()
    # Persist ownership immediately, even if initialization is interrupted.
    with os.fdopen(fd, "w") as stream:
        stream.write("export TEST_DATABASE_URL=" + shlex.quote(
            url.render_as_string(hide_password=False)
        ) + "\nexport SITG_TEST_DATABASE_TOKEN=" + shlex.quote(token) + "\n")
    conn = await connect(url)
    try:
        await conn.execute(
            "CREATE SCHEMA sitg_test_control; "
            "CREATE TABLE sitg_test_control.owner "
            "(token_hash text PRIMARY KEY, closing boolean NOT NULL DEFAULT false)"
        )
        await conn.execute("INSERT INTO sitg_test_control.owner(token_hash) VALUES ($1)",
                           hashlib.sha256(token.encode()).hexdigest())
    finally:
        await conn.close()
    print(f"Created {name}; private ownership record: {record}")


async def run_consumer(command):
    url, digest = target()
    conn = await connect(url)
    child = None
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK, timeout=2400)
        await owned(conn, digest)
        env = {**os.environ, "SITG_TEST_DATABASE_LEASE": str(conn.get_server_pid())}
        # Never let an inherited application DATABASE_URL reach test migrations.
        env["DATABASE_URL"] = url.render_as_string(hide_password=False)
        child = await asyncio.create_subprocess_exec(*command, env=env, start_new_session=True)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda sig=sig: os.killpg(child.pid, sig))
        return await child.wait()
    finally:
        if child is not None:
            # A live HTTP/browser consumer must keep its entire lifetime inside
            # this command. Do not release the lease with a server left behind.
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
        await conn.close()


async def cleanup():
    url, digest = target()
    conn = await connect(url)
    try:
        await owned(conn, digest, allow_closing=True)
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK):
            raise RuntimeError("Database has an active consumer; cleanup refused")
        # Refuse rather than terminating sessions, including unmanaged consumers.
        if await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid()"
        ):
            raise RuntimeError("Database still has connected consumers; cleanup refused")
        await conn.execute("UPDATE sitg_test_control.owner SET closing = true")
    finally:
        await conn.close()
    admin = await connect(url.set(database="postgres"))
    try:
        await admin.execute(f'DROP DATABASE "{url.database}"')
    finally:
        await admin.close()
    print(f"Dropped {url.database}; retain the private record as the cleanup receipt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    provision = commands.add_parser("create")
    provision.add_argument("--name", required=True)
    provision.add_argument("--record", required=True)
    run = commands.add_parser("run")
    run.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("cleanup")
    args = parser.parse_args()
    if args.action == "create":
        asyncio.run(create(args.name, args.record))
    elif args.action == "cleanup":
        asyncio.run(cleanup())
    else:
        command = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            parser.error("run requires a command after --")
        sys.exit(asyncio.run(run_consumer(command)))


if __name__ == "__main__":
    main()
