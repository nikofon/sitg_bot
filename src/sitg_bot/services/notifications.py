from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerNotificationAlertRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
)

NOTIFICATION_AUDIENCES = frozenset({"player", "manager", "admin"})
NOTIFICATION_ALERT_COOLDOWN = timedelta(seconds=15)


class NotificationWriter:
    """Creates per-recipient notifications and schedules throttled Telegram alerts."""

    @classmethod
    async def create_for_player(
        cls,
        session: AsyncSession,
        *,
        recipient_player_id: UUID,
        audience: str,
        kind: str,
        deduplication_key: str,
        payload: Mapping[str, Any],
    ) -> PlayerNotificationRecord:
        if audience not in NOTIFICATION_AUDIENCES:
            raise ValueError("Unknown notification audience")
        notification = PlayerNotificationRecord(
            recipient_player_id=recipient_player_id,
            audience=audience,
            kind=kind,
            deduplication_key=deduplication_key,
            payload=dict(payload),
        )
        session.add(notification)
        await session.flush()
        await cls._schedule_alert(
            session,
            recipient_player_id=recipient_player_id,
            audience=audience,
            deduplication_key=deduplication_key,
        )
        return notification

    @classmethod
    async def create_for_administrators(
        cls,
        session: AsyncSession,
        *,
        kind: str,
        deduplication_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[PlayerNotificationRecord, ...]:
        administrators = tuple(
            (
                await session.execute(
                    select(PlayerRecord)
                    .join(
                        PlatformAdministratorRecord,
                        PlatformAdministratorRecord.player_id == PlayerRecord.id,
                    )
                    .where(
                        PlatformAdministratorRecord.revoked_at.is_(None),
                        PlayerRecord.status == "active",
                    )
                    .order_by(PlayerRecord.id)
                )
            ).scalars()
        )
        notifications: list[PlayerNotificationRecord] = []
        for administrator in administrators:
            recipient_key = f"{deduplication_key}:admin:{administrator.id}"
            notifications.append(
                await cls.create_for_player(
                    session,
                    recipient_player_id=administrator.id,
                    audience="admin",
                    kind=kind,
                    deduplication_key=recipient_key,
                    payload=payload,
                )
            )
        return tuple(notifications)

    @classmethod
    async def _schedule_alert(
        cls,
        session: AsyncSession,
        *,
        recipient_player_id: UUID,
        audience: str,
        deduplication_key: str,
    ) -> None:
        now = datetime.now(UTC)
        player = await session.scalar(
            select(PlayerRecord)
            .where(
                PlayerRecord.id == recipient_player_id,
                PlayerRecord.status == "active",
            )
            .with_for_update()
        )
        if player is None or player.telegram_user_id is None:
            return
        if await TelegramNavigationService._active_game(session, recipient_player_id) is not None:
            return
        alert = await session.get(
            PlayerNotificationAlertRecord,
            recipient_player_id,
            with_for_update=True,
        )
        if alert is not None and alert.last_alerted_at > now - NOTIFICATION_ALERT_COOLDOWN:
            return
        navigation = await session.get(PlayerTelegramNavigationRecord, recipient_player_id)
        saved_mode = navigation.interaction_mode if navigation is not None else "player"
        available_modes = await TelegramNavigationService._available_modes(
            session, recipient_player_id
        )
        active_mode = saved_mode if saved_mode in available_modes else "player"
        if alert is None:
            session.add(
                PlayerNotificationAlertRecord(
                    player_id=recipient_player_id,
                    last_alerted_at=now,
                )
            )
        else:
            alert.last_alerted_at = now
        await TransactionalOutbox.enqueue(
            session,
            topic="telegram.notification.alert",
            deduplication_key=f"notification-alert:{deduplication_key}",
            partition_key=f"telegram:chat:{player.telegram_user_id}",
            payload={
                "recipient_telegram_user_id": player.telegram_user_id,
                "locale": player.preferred_locale,
                "audience": audience,
                "same_role": active_mode == audience,
            },
        )
