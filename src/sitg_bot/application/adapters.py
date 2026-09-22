from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from sitg_bot.application.contracts import (
    ActionCode,
    ApplicationPrincipal,
    GatewayOperation,
    GatewayRequest,
    GatewayResponse,
    NotificationReadOperation,
    PacketDraftTelegramBindOperation,
    RequestMetadata,
)
from sitg_bot.application.telegram import TelegramUpdateClaim

if TYPE_CHECKING:
    from sitg_bot.services.miniapp_auth import MiniAppSessionContext, PublicBrowserContext


class GatewayExecutor(Protocol):
    async def execute(
        self, principal: ApplicationPrincipal, request: GatewayRequest
    ) -> GatewayResponse: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedTelegramIdentity:
    """Identity produced only after Telegram update or Mini App init-data verification."""

    telegram_user_id: int

    def principal(self) -> ApplicationPrincipal:
        return ApplicationPrincipal(telegram_user_id=self.telegram_user_id)


class TelegramGatewayAdapter:
    """Thin bot adapter; handlers pass typed operations and never access storage."""

    def __init__(
        self,
        gateway: GatewayExecutor,
        *,
        client_version: str,
        bot_id: int,
        environment: str,
    ) -> None:
        self.gateway = gateway
        self.client_version = client_version
        self.bot_id = bot_id
        self.environment = environment

    async def execute_update(
        self,
        claim: TelegramUpdateClaim,
        operation: GatewayOperation,
    ) -> GatewayResponse:
        return await self._execute(claim, operation)

    async def execute_callback(
        self,
        claim: TelegramUpdateClaim,
        operation: GatewayOperation,
    ) -> GatewayResponse:
        return await self._execute(claim, operation)

    async def _execute(
        self,
        claim: TelegramUpdateClaim,
        operation: GatewayOperation,
    ) -> GatewayResponse:
        if claim.bot_id != self.bot_id or claim.environment != self.environment:
            raise PermissionError("Telegram update claim belongs to another bot environment")
        idempotency_key = f"telegram-update:{claim.bot_id}:{claim.environment}:{claim.update_id}"
        if isinstance(operation, PacketDraftTelegramBindOperation):
            # One document update can produce several independently bound draft messages.
            idempotency_key += f":draft:{operation.draft_id}"
        if isinstance(operation, NotificationReadOperation):
            # One menu action displays many notifications; each is marked read separately.
            idempotency_key += f":notification:{operation.notification_id}"
        request = GatewayRequest(
            metadata=RequestMetadata(
                correlation_id=claim.correlation_id,
                channel="telegram_bot",
                client_name="sitg-telegram-bot",
                client_version=self.client_version,
                idempotency_key=idempotency_key,
            ),
            operation=operation,
        )
        principal = ApplicationPrincipal(telegram_user_id=claim.telegram_user_id)
        return await self.gateway.execute(principal, request)


class MiniAppGatewayAdapter:
    """Transport shim for verified Mini App calls into the shared gateway."""

    def __init__(self, gateway: GatewayExecutor, *, client_version: str) -> None:
        self.gateway = gateway
        self.client_version = client_version

    async def execute(
        self,
        session: MiniAppSessionContext | PublicBrowserContext,
        operation: GatewayOperation,
        *,
        correlation_id: UUID | None = None,
    ) -> GatewayResponse:
        if ActionCode(operation.action) != session.action:
            raise PermissionError("Mini App session was authorized for another action")
        request = GatewayRequest(
            metadata=RequestMetadata(
                correlation_id=correlation_id or uuid4(),
                channel="mini_app",
                client_name="sitg-mini-app",
                client_version=self.client_version,
                idempotency_key=session.idempotency_key,
            ),
            operation=operation,
        )
        return await self.gateway.execute(session.principal, request)
