"""Shared fixtures for agent C's training AI integration tests.

Creates a disposable PostgreSQL database with the shared schema (via Alembic)
including the integrated knowledge and analysis migrations.
"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sitg_bot.services.entitlements import EntitlementService
from sitg_bot.storage.database import Database, normalize_database_url
from sitg_bot.storage.models import PlayerRecord


@pytest.fixture
def training_database(monkeypatch):
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    source_url = normalize_database_url(source_url)
    name = f"sitg_training_{uuid4().hex}"

    async def manage(statement):
        engine = create_async_engine(source_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(manage(f'CREATE DATABASE "{name}"'))
    url = make_url(source_url).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")

        yield url
    finally:
        asyncio.run(manage(f'DROP DATABASE "{name}"'))


@pytest.fixture
async def database(training_database):
    database = Database(training_database)
    try:
        yield database
    finally:
        await database.close()


async def create_player(database: Database, telegram_user_id: int, nickname: str) -> UUID:
    async with database.transaction() as session:
        player = PlayerRecord(
            telegram_user_id=telegram_user_id,
            real_name=f"Real {nickname}",
            public_nickname=nickname,
            telegram_username=nickname,
            status="active",
            registration_step="complete",
            registration_completed_at=datetime.now(UTC),
        )
        session.add(player)
        await session.flush()
        return player.id


async def seed_category(
    database: Database, code: str, payload: dict, player_ids: list[UUID]
) -> UUID:
    entitlements = EntitlementService(database)
    async with database.transaction() as session:
        version = await entitlements.create_policy(session, payload)
        category_id = await entitlements.create_category(
            session, code=code, display_name=code, policy_version=version
        )
        for player_id in player_ids:
            await entitlements.assign_category(
                session, player_id=player_id, category_id=category_id
            )
    return category_id
