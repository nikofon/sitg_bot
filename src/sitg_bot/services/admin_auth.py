import hashlib
import hmac
from uuid import UUID

from sqlalchemy import select

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
)


class PlatformAdminAuthenticationService:
    """Promotes a verified Telegram player after checking the server-held credential."""

    def __init__(self, database: Database, *, credential: str) -> None:
        if len(credential) < 32:
            raise ValueError("Administrator credential must contain at least 32 characters")
        self.database = database
        self._credential_digest = hashlib.sha256(credential.encode()).digest()

    async def authenticate(
        self,
        player_id: UUID,
        telegram_user_id: int,
        credential: str,
    ) -> None:
        supplied_digest = hashlib.sha256(credential.encode()).digest()
        if not hmac.compare_digest(self._credential_digest, supplied_digest):
            raise PermissionError("Administrator credential is invalid")

        async with self.database.transaction() as session:
            player = await session.scalar(
                select(PlayerRecord)
                .where(
                    PlayerRecord.id == player_id,
                    PlayerRecord.telegram_user_id == telegram_user_id,
                )
                .with_for_update()
            )
            if player is None or player.status != "active":
                raise PermissionError("Completed player registration is required")

            administrator = await session.get(
                PlatformAdministratorRecord, player_id, with_for_update=True
            )
            if administrator is None:
                session.add(PlatformAdministratorRecord(player_id=player_id))
            else:
                administrator.revoked_at = None

            navigation = await session.get(
                PlayerTelegramNavigationRecord, player_id, with_for_update=True
            )
            if navigation is None:
                session.add(
                    PlayerTelegramNavigationRecord(
                        player_id=player_id,
                        interaction_mode="admin",
                        version=1,
                    )
                )
            elif navigation.interaction_mode != "admin":
                navigation.interaction_mode = "admin"
                navigation.version += 1
