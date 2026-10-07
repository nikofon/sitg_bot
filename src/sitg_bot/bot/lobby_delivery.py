import asyncio
from collections import defaultdict
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
from sitg_bot.bot.keyboards.common import joined_lobby_keyboard
from sitg_bot.bot.miniapps import mini_app_launch_url
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel
from sitg_bot.bot.presenters.players import player_name
from sitg_bot.bot.presenters.render import DISABLED_LINK_PREVIEW, telegram_keyboard
from sitg_bot.bot.state.models import NavigationState
from sitg_bot.services.launch_references import LaunchReference


class LobbyDelivery:
    """Keep one durable summary/settings pair per lobby member."""

    def __init__(self, bot, localization, protocol, base_url, bot_username):
        self.bot = bot
        self.localization = localization
        self.protocol = protocol
        self.base_url = base_url
        self.bot_username = bot_username
        self.locks = defaultdict(asyncio.Lock)

    async def __call__(self, payload):
        try:
            await self.show(
                int(payload["recipient_telegram_user_id"]), payload["lobby_id"],
                self.localization.locale(str(payload.get("locale", "ru"))),
                join_sequence=payload.get("join_sequence"),
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    async def show(
        self, chat, lobby_id, locale, *, replace=False, keyboard=None, join_sequence=None,
    ):
        from sitg_bot.bot.handlers.lobby import lobby_info_text

        async with self.locks[chat]:
            params = {"telegram_user_id": chat, "lobby_id": str(lobby_id)}
            state = await self.protocol.request(
                "telegram.lobby.presentation", **params,
                force=replace or join_sequence is not None,
            )
            if not state["active"]:
                return
            messages = dict(state["messages"])
            if join_sequence is not None and messages.get("join_sequence") != join_sequence:
                replace = True

            async def save():
                await self.protocol.request("telegram.lobby.record", **params, messages=messages)

            async def delete(key):
                try:
                    await self.bot.delete_message(chat, messages[key])
                except TelegramBadRequest as error:
                    if "message to delete not found" not in str(error).lower():
                        raise
                del messages[key]
                await save()

            if replace:
                for key in ("navigation", "summary", "settings"):
                    if key not in messages:
                        continue
                    await delete(key)
            text = (
                lobby_info_text(state["lobby"], self.localization, locale, self.bot_username)
                if "lobby" in state else messages["summary_text"]
            )
            if "summary" in messages and messages.get("summary_text") != text:
                try:
                    await self.bot.edit_message_text(
                        text, chat_id=chat, message_id=messages["summary"],
                        link_preview_options=DISABLED_LINK_PREVIEW,
                    )
                except TelegramBadRequest as error:
                    reason = str(error).lower()
                    if "message can't be edited" in reason:
                        await delete("summary")
                    elif "message to edit not found" in reason:
                        del messages["summary"]
                        await save()
                    elif "message is not modified" not in reason:
                        raise
            if "summary" not in messages:
                if "navigation" not in messages:
                    if keyboard is None:
                        navigation = await self.protocol.request(
                            "telegram.navigation.snapshot", telegram_user_id=chat,
                        )
                        keyboard = menu_message(
                            NavigationState.model_validate(navigation), self.localization, locale,
                        ).keyboard
                    # Telegram cannot edit a message carrying a reply keyboard.
                    sent = await self.bot.send_message(
                        chat, self.localization.text("lobby.menu", locale),
                        reply_markup=telegram_keyboard(keyboard),
                    )
                    messages["navigation"] = sent.message_id
                    await save()
                sent = await self.bot.send_message(
                    chat, text,
                    link_preview_options=DISABLED_LINK_PREVIEW,
                )
                messages["summary"] = sent.message_id
                await save()
            if messages.get("summary_text") != text:
                messages["summary_text"] = text
                await save()
            if self.base_url and "settings" not in messages:
                reference = LaunchReference(
                    state["launch_reference"], datetime.fromisoformat(state["expires_at"]),
                )
                url = mini_app_launch_url(self.base_url, "lobbies", reference) + "&section=settings"
                sent = await self.bot.send_message(
                    chat, self.localization.text("lobby.info.settings_prompt", locale),
                    reply_markup=telegram_keyboard(InlineKeyboardModel(rows=((InlineButtonModel(
                        self.localization.text("button.lobby.settings", locale), web_app_url=url,
                    ),),))),
                )
                messages["settings"] = sent.message_id
                await save()
            if join_sequence is not None and messages.get("join_sequence") != join_sequence:
                messages["join_sequence"] = join_sequence
                await save()
            if messages.get("summary_version") != state["version"]:
                messages["summary_version"] = state["version"]
                await save()


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


def lobby_notice_delivery_handler(
    bot: Bot, localization: LocalizationService, bot_username=None, *, protocol=None,
):
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
                        name=player_name(member, bot_username),
                        status=localization.text(
                            "button.lobby.ready"
                            if member.get("ready")
                            else "button.lobby.unready",
                            locale,
                        ),
                    )
                    for member in statuses
                )
        elif kind == "kicked_self":
            text = localization.text("lobby.kicked_self", locale)
            navigation = await protocol.request(
                "telegram.navigation.snapshot",
                telegram_user_id=int(payload["recipient_telegram_user_id"]),
            )
            keyboard = telegram_keyboard(menu_message(
                NavigationState.model_validate(navigation), localization, locale,
            ).keyboard)
        elif kind == "player_left" and payload.get("kicked"):
            text = localization.text("lobby.player_kicked", locale, name=payload["player_name"])
        elif kind in {"player_joined", "player_left"}:
            text = localization.text(
                f"lobby.{kind}",
                locale,
                name=payload["player_name"],
                role=localization.text(f"lobby.role.{payload['role']}", locale),
            )
            joining_telegram_user_id = payload.get("player_telegram_user_id")
            if (
                kind == "player_joined"
                and joining_telegram_user_id is not None
                and int(joining_telegram_user_id) == int(payload["recipient_telegram_user_id"])
            ):
                # A member who joined through the "Ongoing games" Mini App view returns
                # to a chat whose reply keyboard still shows the previous context; the
                # join notice installs the lobby controls for the new navigation context.
                keyboard = telegram_keyboard(
                    joined_lobby_keyboard(str(payload.get("role", "player")), localization, locale)
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
                int(payload["recipient_telegram_user_id"]),
                text,
                reply_markup=keyboard,
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver
