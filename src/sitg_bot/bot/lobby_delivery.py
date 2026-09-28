import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from sitg_bot.application.protocol import (
    RemoteOutboxConsumer,
    RetryableDeliveryError,
    TerminalDeliveryError,
)
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.miniapps import mini_app_launch_url
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel
from sitg_bot.bot.presenters.render import telegram_keyboard
from sitg_bot.services.launch_references import LaunchReference


def lobby_open_delivery_handler(
    bot: Bot,
    localization: LocalizationService,
    base_url: str,
) -> Callable[[dict[str, object]], Awaitable[None]]:
    async def deliver(payload: dict[str, object]) -> None:
        try:
            telegram_user_id = int(payload["recipient_telegram_user_id"])
            locale = localization.locale(str(payload["locale"]))
            reference = LaunchReference(
                str(payload["launch_reference"]),
                datetime.fromisoformat(str(payload["expires_at"])),
            )
            url = mini_app_launch_url(base_url, "lobbies", reference)
            keyboard = InlineKeyboardModel(
                rows=(
                    (
                        InlineButtonModel(
                            localization.text("button.lobby.open", locale),
                            web_app_url=url,
                        ),
                    ),
                )
            )
            await bot.send_message(
                telegram_user_id,
                localization.text("lobby.open_prompt", locale),
                reply_markup=telegram_keyboard(keyboard),
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver


def notification_alert_delivery_handler(
    bot: Bot,
    localization: LocalizationService,
) -> Callable[[dict[str, object]], Awaitable[None]]:
    async def deliver(payload: dict[str, object]) -> None:
        try:
            telegram_user_id = int(payload["recipient_telegram_user_id"])
            locale = localization.locale(str(payload.get("locale", "ru")))
            audience = str(payload["audience"])
            same_role = bool(payload["same_role"])
            if audience not in {"player", "manager", "admin"}:
                raise TerminalDeliveryError("Notification audience is invalid")
            if same_role:
                text = localization.text("notifications.new_alert", locale)
            else:
                text = localization.text(
                    "notifications.new_role_alert",
                    locale,
                    role=localization.text(f"notification.role.{audience}", locale),
                )
            await bot.send_message(
                telegram_user_id,
                text,
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver


def packet_draft_status_delivery_handler(
    bot: Bot,
    localization: LocalizationService,
) -> Callable[[dict[str, object]], Awaitable[None]]:
    async def deliver(payload: dict[str, object]) -> None:
        status = str(payload["status"])
        if status not in {"published", "rejected"}:
            raise TerminalDeliveryError("Packet draft status is invalid")
        chat_id = int(payload["chat_id"])
        message_id = int(payload["message_id"])
        locale = localization.locale(str(payload.get("locale", "ru")))
        try:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
            except TelegramBadRequest as error:
                # A direct bot callback may have removed the keyboard and sent the
                # confirmation while this event was waiting in the outbox.
                if "message is not modified" in str(error).casefold():
                    return
            await bot.send_message(
                chat_id,
                localization.text(f"packet_upload.{status}", locale),
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver


async def run_outbox_consumer(consumer: RemoteOutboxConsumer) -> None:
    while True:
        try:
            delivered = await consumer.run_once(limit=20)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(0.1 if delivered else 1)


def lobby_notice_delivery_handler(bot: Bot, localization: LocalizationService):
    async def deliver(payload: dict[str, object]) -> None:
        locale = localization.locale(str(payload.get("locale", "ru")))
        kind = str(payload["kind"])
        keyboard = None
        if kind == "invitation":
            code = str(payload["invitation_code"])
            rows = [
                (
                    InlineButtonModel(
                        localization.text("button.lobby.join_player", locale),
                        callback_data=f"lobbyjoin:p:{code}",
                    ),
                )
            ]
            if payload.get("allow_observer"):
                rows.append(
                    (
                        InlineButtonModel(
                            localization.text("button.lobby.join_observer", locale),
                            callback_data=f"lobbyjoin:o:{code}",
                        ),
                    )
                )
            keyboard = telegram_keyboard(InlineKeyboardModel(rows=tuple(rows)))
            text = localization.text(
                "lobby.invitation",
                locale,
                sender=payload["sender"],
                tournament=payload["tournament"],
            )
        elif kind == "settings_changed":
            # Field labels share the Mini App's localized descriptor vocabulary.
            changes = payload.get("changes", {})
            text = localization.text(
                "lobby.settings_changed",
                locale,
                changes="\n".join(
                    f"{localization.catalogs[locale].get('lobby.setting.' + key, key)}: {value}"
                    for key, value in changes.items()
                ),
            )
        elif kind == "readiness_changed":
            statuses = payload.get("members")
            if statuses is None:
                text = localization.text(
                    "lobby.player_ready" if payload["ready"] else "lobby.player_not_ready",
                    locale,
                    name=payload["player_name"],
                    ready_count=payload["ready_count"],
                    player_count=payload["player_count"],
                )
            else:
                text = localization.text(
                    "lobby.readiness.status",
                    locale,
                    ready_count=payload.get("ready_count", 0),
                    player_count=payload.get("player_count", len(statuses)),
                ) + "\n" + "\n".join(
                    localization.text(
                        "lobby.readiness.player",
                        locale,
                        icon="✅" if member.get("ready") else "⏳",
                        name=member.get("name", ""),
                        status=localization.text(
                            "button.lobby.ready"
                            if member.get("ready")
                            else "button.lobby.unready",
                            locale,
                        ),
                    )
                    for member in statuses
                )
        elif kind in {"player_joined", "player_left"}:
            text = localization.text(
                f"lobby.{kind}",
                locale,
                name=payload["player_name"],
                role=localization.text(f"lobby.role.{payload['role']}", locale),
            )
        elif kind == "lobby_cancelled":
            text = localization.text("lobby.cancelled", locale)
        elif kind == "packet_removed":
            text = localization.text(
                "lobby.packet_removed", locale, packet_name=payload["packet_name"]
            )
        elif kind == "packet_rejected":
            text = localization.text(
                "lobby.packet_rejected",
                locale,
                packet_name=payload["packet_name"],
                reason=localization.text(f"lobby.reason.{payload['reason']}", locale),
            )
        else:
            text = localization.text(
                "lobby.packet_selected", locale, packet_name=payload["packet_name"]
            )
            if payload.get("classic_players"):
                key = (
                    "lobby.classic_solo" if payload.get("classic_solo") else "lobby.classic_players"
                )
                text += "\n" + localization.text(
                    key, locale, players=", ".join(payload["classic_players"])
                )
        try:
            await bot.send_message(
                int(payload["recipient_telegram_user_id"]), text, reply_markup=keyboard
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver
