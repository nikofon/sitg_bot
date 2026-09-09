import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sitg_bot.application.contracts import (
    ApplicationPrincipal,
    GatewayRequest,
    GatewayResponse,
)
from sitg_bot.application.telegram import TelegramUpdateClaim

MAX_PROTOCOL_BYTES = 8 * 1024 * 1024


class ApplicationProtocolError(RuntimeError):
    pass


class ApplicationProtocolClient:
    """Concurrent authenticated NDJSON client for presentation processes."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_name: str,
        channel: str,
        credential: str,
        bot_id: int | None = None,
        environment: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_name = client_name
        self.channel = channel
        self.credential = credential
        self.bot_id = bot_id
        self.environment = environment
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.writer is not None:
            return
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, limit=MAX_PROTOCOL_BYTES
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"{self.client_name}-application-reader"
        )
        try:
            await self.request(
                "adapter.authenticate",
                client_name=self.client_name,
                channel=self.channel,
                credential=self.credential,
                bot_id=self.bot_id,
                environment=self.environment,
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        writer = self.writer
        self.writer = None
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def request(self, action: str, **params: Any) -> Any:
        if self.writer is None:
            raise ConnectionError("Application protocol client is not connected")
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        encoded = json.dumps(
            {"id": request_id, "action": action, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_PROTOCOL_BYTES:
            self._pending.pop(request_id, None)
            raise ValueError("Application protocol request is too large")
        try:
            async with self._write_lock:
                assert self.writer is not None
                self.writer.write(encoded + b"\n")
                await self.writer.drain()
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self.reader.at_eof():
                raw = await self.reader.readline()
                if not raw:
                    break
                message = json.loads(raw)
                request_id = message.get("id")
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    error = message.get("error", {})
                    future.set_exception(
                        ApplicationProtocolError(
                            f"{error.get('type', 'ProtocolError')}: "
                            f"{error.get('message', 'Server rejected the request')}"
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(error)
        finally:
            self._fail_pending(ConnectionError("Application server connection closed"))

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class RemoteApplicationGateway:
    """ApplicationGateway-compatible proxy that has no storage dependency."""

    def __init__(self, client: ApplicationProtocolClient) -> None:
        self.client = client

    async def execute(
        self, principal: ApplicationPrincipal, request: GatewayRequest
    ) -> GatewayResponse:
        result = await self.client.request(
            "application",
            principal={
                "player_id": str(principal.player_id) if principal.player_id else None,
                "telegram_user_id": principal.telegram_user_id,
            },
            request=request.model_dump(mode="json"),
        )
        return GatewayResponse.model_validate(result)


class RemoteTelegramUpdateAuthService:
    """Server-backed update receipt service used by the presentation-only bot."""

    def __init__(self, client: ApplicationProtocolClient) -> None:
        self.client = client

    async def claim(
        self,
        *,
        bot_id: int,
        update_id: int,
        telegram_user_id: int,
        correlation_id: UUID | None = None,
    ) -> TelegramUpdateClaim:
        result = await self.client.request(
            "telegram.update.claim",
            bot_id=bot_id,
            update_id=update_id,
            telegram_user_id=telegram_user_id,
            correlation_id=str(correlation_id) if correlation_id else None,
        )
        return TelegramUpdateClaim(
            receipt_id=UUID(result["receipt_id"]),
            correlation_id=UUID(result["correlation_id"]),
            bot_id=int(result["bot_id"]),
            environment=str(result["environment"]),
            update_id=int(result["update_id"]),
            telegram_user_id=int(result["telegram_user_id"]),
        )

    async def complete(
        self,
        receipt_id: UUID,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        await self.client.request(
            "telegram.update.complete",
            receipt_id=str(receipt_id),
            succeeded=succeeded,
            error_code=error_code,
        )


OutboxHandler = Callable[[dict[str, Any]], Awaitable[None]]


class TerminalDeliveryError(RuntimeError):
    """A blocked chat, deleted destination, or other non-retryable delivery failure."""


class RetryableDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RemoteOutboxConsumer:
    """Presentation-side outbox consumer; acknowledgements are lease protected."""

    def __init__(
        self,
        client: ApplicationProtocolClient,
        handlers: dict[str, OutboxHandler],
    ) -> None:
        self.client = client
        self.handlers = dict(handlers)

    async def run_once(self, *, limit: int = 20) -> int:
        if not self.handlers:
            return 0
        deliveries = await self.client.request(
            "outbox.claim", topics=sorted(self.handlers), limit=limit
        )
        for delivery in deliveries:
            event_id = delivery["event_id"]
            lease_token = delivery["lease_token"]
            handler = self.handlers.get(delivery["topic"])
            if handler is None:
                await self.client.request(
                    "outbox.reject",
                    event_id=event_id,
                    lease_token=lease_token,
                    error="No presentation handler is registered",
                    terminal=True,
                )
                continue
            try:
                await handler(delivery["payload"])
            except asyncio.CancelledError:
                raise
            except TerminalDeliveryError as error:
                await self.client.request(
                    "outbox.reject",
                    event_id=event_id,
                    lease_token=lease_token,
                    error=str(error),
                    terminal=True,
                )
            except RetryableDeliveryError as error:
                await self.client.request(
                    "outbox.reject",
                    event_id=event_id,
                    lease_token=lease_token,
                    error=str(error),
                    terminal=False,
                    retry_after_seconds=error.retry_after_seconds,
                )
            except Exception as error:
                await self.client.request(
                    "outbox.reject",
                    event_id=event_id,
                    lease_token=lease_token,
                    error=repr(error),
                    terminal=False,
                )
            else:
                await self.client.request(
                    "outbox.acknowledge",
                    event_id=event_id,
                    lease_token=lease_token,
                )
        return len(deliveries)
