import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sitg_bot.services.admin_auth import PlatformAdminAuthenticationService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
)

pytestmark = pytest.mark.integration


async def _exercise_authentication(database_url: str) -> None:
    database = Database(database_url)
    telegram_user_id = 9_000_000_000 + uuid4().int % 1_000_000_000
    player_id = None
    try:
        async with database.transaction() as session:
            player = PlayerRecord(
                telegram_user_id=telegram_user_id,
                real_name="Admin Test",
                public_nickname="Admin Test",
                registration_step="complete",
                registration_completed_at=datetime.now(UTC),
                status="active",
            )
            session.add(player)
            await session.flush()
            player_id = player.id

        service = PlatformAdminAuthenticationService(
            database, credential="correct-admin-credential-32-chars!"
        )
        with pytest.raises(PermissionError):
            await service.authenticate(
                player_id, telegram_user_id, "incorrect-admin-credential-32!"
            )

        await service.authenticate(
            player_id, telegram_user_id, "correct-admin-credential-32-chars!"
        )
        async with database.transaction() as session:
            administrator = await session.get(PlatformAdministratorRecord, player_id)
            navigation = await session.get(PlayerTelegramNavigationRecord, player_id)
            assert administrator is not None
            assert administrator.revoked_at is None
            assert navigation is not None
            assert navigation.interaction_mode == "admin"
            assert navigation.version == 1
    finally:
        if player_id is not None:
            async with database.transaction() as session:
                navigation = await session.get(PlayerTelegramNavigationRecord, player_id)
                if navigation is not None:
                    await session.delete(navigation)
                administrator = await session.get(PlatformAdministratorRecord, player_id)
                if administrator is not None:
                    await session.delete(administrator)
                await session.flush()
                player = await session.get(PlayerRecord, player_id)
                if player is not None:
                    await session.delete(player)
        await database.close()


def test_telegram_admin_credential_activates_role_and_navigation() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    asyncio.run(_exercise_authentication(database_url))
