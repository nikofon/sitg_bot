import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from sitg_bot.application.adapters import TelegramGatewayAdapter
from sitg_bot.application.protocol import (
    ApplicationProtocolClient,
    RemoteApplicationGateway,
    RemoteOutboxConsumer,
    RemoteTelegramUpdateAuthService,
)
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.callbacks import CallbackReferenceStore
from sitg_bot.bot.chat_delivery import chat_delivery_handler
from sitg_bot.bot.game_delivery import game_delivery_handler
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.lobby_delivery import (
    lobby_notice_delivery_handler,
    lobby_open_delivery_handler,
    notification_alert_delivery_handler,
    packet_draft_status_delivery_handler,
    run_outbox_consumer,
)
from sitg_bot.bot.middleware import (
    CorrelationLoggingMiddleware,
    ErrorMappingMiddleware,
    LocaleMiddleware,
    PlayerContextMiddleware,
    PrivateTelegramIdentityMiddleware,
    TelegramDeduplicationMiddleware,
)
from sitg_bot.bot.router import root_router
from sitg_bot.bot.state import BotBackend
from sitg_bot.config import Settings


class Telemetry(Protocol):
    def count(self, name: str, *, value: int = 1) -> None: ...


class NullTelemetry:
    def count(self, name: str, *, value: int = 1) -> None:
        return None


class TelegramUpdateAuthenticator(Protocol):
    async def claim(
        self,
        *,
        bot_id: int,
        update_id: int,
        telegram_user_id: int,
        correlation_id: UUID | None = None,
    ) -> TelegramUpdateClaim: ...

    async def complete(
        self,
        receipt_id: UUID,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BotDependencies:
    backend: BotBackend
    localization: LocalizationService
    launch_links: Any | None
    clock: Callable[[], datetime]
    telemetry: Telemetry
    callback_references: CallbackReferenceStore = field(default_factory=CallbackReferenceStore)


def create_dispatcher(
    dependencies: BotDependencies,
    auth_service: TelegramUpdateAuthenticator,
) -> Dispatcher:
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dependencies.backend.game_callback_references = dependencies.callback_references
    middleware = dispatcher.update.outer_middleware
    middleware(CorrelationLoggingMiddleware())
    middleware(TelegramDeduplicationMiddleware(auth_service))
    middleware(PrivateTelegramIdentityMiddleware())
    middleware(PlayerContextMiddleware(dependencies.backend))
    middleware(LocaleMiddleware(dependencies.localization))
    middleware(ErrorMappingMiddleware(dependencies.localization))
    dispatcher.include_router(root_router)
    dispatcher["callback_references"] = dependencies.callback_references
    return dispatcher


async def configure_bot_commands(bot: Bot, localization: LocalizationService) -> None:
    command_names = (
        "start",
        "menu",
        "help",
        "cancel",
        "language",
        "set",
        "admin",
        "join",
        "reconnect",
        "score",
        "players",
        "whisper",
        "answer",
        "appeal",
        "appeals",
        "pause",
        "resume",
        "commentary",
        "escalate",
        "abandon",
        "results",
        "report",
        "quit",
    )
    scope = BotCommandScopeAllPrivateChats()
    for locale, language_code in (("ru", None), ("ru", "ru"), ("en", "en")):
        commands = [
            BotCommand(
                command=name,
                description=localization.text(f"command.{name}.description", locale),
            )
            for name in command_names
        ]
        await bot.set_my_commands(
            commands,
            scope=scope,
            language_code=language_code,
        )


async def run_polling(settings: Settings) -> None:
    bot_token = settings.bot_token.get_secret_value()
    bot_id = int(bot_token.split(":", 1)[0])
    if settings.application_client_token is None:
        raise RuntimeError("SITG_APPLICATION_CLIENT_TOKEN is required by the Telegram bot")
    protocol = ApplicationProtocolClient(
        settings.application_server_host,
        settings.application_server_port,
        client_name="sitg-telegram-bot",
        channel="telegram_bot",
        credential=settings.application_client_token.get_secret_value(),
        bot_id=bot_id,
        environment=settings.telegram_environment,
    )
    await protocol.connect()
    gateway = TelegramGatewayAdapter(
        RemoteApplicationGateway(protocol),
        client_version=settings.application_client_version,
        bot_id=bot_id,
        environment=settings.telegram_environment,
    )

    def clock() -> datetime:
        return datetime.now(UTC)

    dependencies = BotDependencies(
        backend=BotBackend(gateway),
        localization=LocalizationService(),
        launch_links=settings.mini_app_base_url,
        clock=clock,
        telemetry=NullTelemetry(),
        callback_references=CallbackReferenceStore(clock=clock),
    )
    dispatcher = create_dispatcher(
        dependencies,
        RemoteTelegramUpdateAuthService(protocol),
    )
    defaults = DefaultBotProperties(parse_mode=ParseMode.HTML)
    try:
        async with Bot(bot_token, default=defaults) as bot:
            await configure_bot_commands(bot, dependencies.localization)
            game_delivery = game_delivery_handler(
                bot, dependencies.localization, protocol, dependencies.callback_references
            )
            dependencies.backend.game_delivery = game_delivery
            handlers = {
                "telegram.chat.message": chat_delivery_handler(
                    bot, dependencies.localization, protocol, game_delivery
                ),
                "game.event": game_delivery,
                "telegram.game.cleanup": game_delivery.cleanup,
                "telegram.lobby.notice": lobby_notice_delivery_handler(
                    bot, dependencies.localization
                ),
                "telegram.notification.alert": notification_alert_delivery_handler(
                    bot, dependencies.localization
                ),
                "telegram.packet.draft_status": packet_draft_status_delivery_handler(
                    bot, dependencies.localization
                ),
            }
            if settings.mini_app_base_url is not None:
                handlers["telegram.lobby.open"] = lobby_open_delivery_handler(
                    bot,
                    dependencies.localization,
                    settings.mini_app_base_url,
                )
            consumer = RemoteOutboxConsumer(protocol, handlers)
            delivery_task = asyncio.create_task(run_outbox_consumer(consumer))
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    backend=dependencies.backend,
                    localization=dependencies.localization,
                    launch_links=dependencies.launch_links,
                    clock=dependencies.clock,
                    telemetry=dependencies.telemetry,
                    callback_references=dependencies.callback_references,
                )
            finally:
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task
    finally:
        await protocol.close()
