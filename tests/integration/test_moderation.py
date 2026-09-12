import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sitg_bot.services.moderation import BugReportService, PlayerModerationService
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.trust import TrustService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlayerNotificationRecord,
    PlayerRecord,
    PlatformAdministratorRecord,
    SuspicionLedgerRecord,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def migrated_database(monkeypatch):
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    name = f"sitg_moderation_{uuid4().hex}"

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
    command.upgrade(config, "head")
    try:
        yield url
    finally:
        asyncio.run(manage(f'DROP DATABASE "{name}"'))


async def _create_player(
    database: Database, *, nickname: str, administrator: bool = False
) -> dict[str, object]:
    async with database.transaction() as session:
        player = PlayerRecord(
            telegram_user_id=9_000_000_000 + uuid4().int % 1_000_000_000,
            real_name=f"Real {nickname}",
            public_nickname=nickname,
            telegram_username=nickname.lower(),
            registration_step="complete",
            registration_completed_at=datetime.now(UTC),
            status="active",
        )
        session.add(player)
        await session.flush()
        if administrator:
            session.add(PlatformAdministratorRecord(player_id=player.id))
        return {"player_id": player.id, "telegram_user_id": player.telegram_user_id}


def test_ban_unban_bug_reports_and_suspicion_ledger(migrated_database) -> None:
    asyncio.run(_exercise(migrated_database))


async def _exercise(database_url: str) -> None:
    database = Database(database_url)
    moderation = PlayerModerationService(database)
    bug_reports = BugReportService(database)
    trust = TrustService(database)
    navigation_service = TelegramNavigationService(database)
    try:
        admin = await _create_player(database, nickname="Admin", administrator=True)
        cheater = await _create_player(database, nickname="Cheater")
        reporter = await _create_player(database, nickname="Reporter")
        target = await _create_player(database, nickname="Target")
        admin_id = admin["player_id"]
        cheater_id = cheater["player_id"]

        receipt = await moderation.ban_player(
            admin_id, "@target", reason="Cheating in games"
        )
        assert receipt.player_id == target["player_id"]
        assert receipt.reason == "Cheating in games"

        with pytest.raises(ValueError):
            await moderation.ban_player(admin_id, str(target["player_id"]), reason="again")
        with pytest.raises(PermissionError):
            await moderation.ban_player(admin_id, "admin", reason="no")
        with pytest.raises(LookupError):
            await moderation.ban_player(admin_id, "@missing", reason="x")

        snapshot = await navigation_service.snapshot(target["telegram_user_id"])
        assert snapshot is not None
        assert snapshot.ban_reason == "Cheating in games"
        assert snapshot.allowed_actions == (
            "start",
            "menu",
            "help",
            "language",
            "player.library",
        )

        bug_receipt = await bug_reports.submit(
            reporter["player_id"], "The scoreboard shows wrong scores"
        )
        assert bug_receipt.reporter_player_id == reporter["player_id"]
        async with database.transaction() as session:
            notifications = list(
                (
                    await session.execute(
                        select(PlayerNotificationRecord).where(
                            PlayerNotificationRecord.recipient_player_id == admin_id,
                            PlayerNotificationRecord.kind == "bug_report",
                        )
                    )
                ).scalars()
            )
        assert len(notifications) == 1
        assert notifications[0].audience == "admin"
        assert notifications[0].payload["reporter_nickname"] == "Reporter"
        assert notifications[0].payload["commentary"] == "The scoreboard shows wrong scores"
        assert notifications[0].payload["created_at"]

        async with database.transaction() as session:
            session.add(
                SuspicionLedgerRecord(
                    player_id=cheater_id,
                    ruleset_key="si",
                    suspicion_before=0,
                    delta=4,
                    suspicion_after=4,
                    reason="si_evaluation",
                )
            )
            target_player = await session.get(PlayerRecord, target["player_id"])
            target_player.suspicion = 7
            cheater_player = await session.get(PlayerRecord, cheater_id)
            cheater_player.suspicion = 4

        ledger = await trust.suspicion_ledger(admin_id)
        assert [item["player_id"] for item in ledger["items"]] == [cheater_id]
        card = ledger["items"][0]
        assert card["suspicion"] == 4
        assert card["display_name"] == "Cheater"
        assert card["rulesets"] == []

        with pytest.raises(PermissionError):
            await trust.suspicion_ledger(reporter["player_id"])

        inspection = await trust.suspicion_inspection(admin_id, cheater_id)
        assert inspection["player"]["suspicion"] == 4
        assert len(inspection["events"]) == 1
        assert inspection["events"][0]["delta"] == 4
        assert inspection["events"][0]["reason"] == "si_evaluation"

        cleared = await trust.clear_player_suspicion(admin_id, cheater_id, note="reviewed")
        assert cleared.suspicion_after == 0

        unban = await moderation.unban_player(admin_id, "@target")
        assert unban.player_id == target["player_id"]
        with pytest.raises(ValueError):
            await moderation.unban_player(admin_id, "@target")

        snapshot = await navigation_service.snapshot(target["telegram_user_id"])
        assert snapshot is not None
        assert snapshot.ban_reason is None
    finally:
        await database.close()
