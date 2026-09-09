from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from sitg_bot.application.contracts import (
    ActionCode,
    ApplicationPrincipal,
    CapabilitiesOperation,
    GatewayRequest,
    RequestMetadata,
)
from sitg_bot.application.protocol import RemoteApplicationGateway
from sitg_bot.server import ConsoleApplicationServer
from sitg_bot.services.reliable_delivery import (
    ClaimedJob,
    DurableJobWorker,
    retry_delay,
)
from sitg_bot.services.reliable_scheduling import AutomaticJobScheduler
from sitg_bot.storage.models import DurableJobRecord, OutboxEventRecord


def test_reliable_records_have_distinct_persistent_tables() -> None:
    assert OutboxEventRecord.__tablename__ == "outbox_events"
    assert DurableJobRecord.__tablename__ == "durable_jobs"
    assert OutboxEventRecord.__table__.c.deduplication_key.unique
    assert DurableJobRecord.__table__.c.deduplication_key.unique
    assert "lease_token" in OutboxEventRecord.__table__.c
    assert "lease_token" in DurableJobRecord.__table__.c


def test_retry_backoff_is_exponential_and_capped() -> None:
    assert retry_delay(1) == timedelta(seconds=1)
    assert retry_delay(5) == timedelta(seconds=16)
    assert retry_delay(100) == timedelta(hours=1)

    with pytest.raises(ValueError, match="positive"):
        retry_delay(0)


def test_scheduler_selects_the_earliest_authoritative_deadline() -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    game = SimpleNamespace(
        join_deadline=now + timedelta(minutes=5),
        pause_abandonment_deadline=None,
        progression_deadline=now + timedelta(seconds=2),
        answer_deadline=None,
        buzz_deadline=now + timedelta(seconds=10),
    )
    appeal = SimpleNamespace(
        status="awaiting_commentary",
        vote_deadline=now,
        escalation_deadline=None,
        commentary_deadline=now + timedelta(minutes=1),
        ticket_expires_at=None,
    )

    assert AutomaticJobScheduler._game_deadline(game) == now + timedelta(seconds=2)
    assert AutomaticJobScheduler._appeal_deadline(appeal) == now + timedelta(minutes=1)


def test_server_adapter_authentication_is_explicit_and_channel_bound() -> None:
    credential = "correct-application-secret-32-characters"
    server = ConsoleApplicationServer(
        object(),  # type: ignore[arg-type]
        application_client_token=credential,
    )
    connection = SimpleNamespace(adapter_session=None)

    result = server._authenticate_adapter(
        connection,  # type: ignore[arg-type]
        {
            "client_name": "sitg-telegram-bot",
            "channel": "telegram_bot",
            "credential": credential,
            "bot_id": 42,
            "environment": "test",
        },
    )

    assert result == {"authenticated": True, "channel": "telegram_bot"}
    assert connection.adapter_session.bot_id == 42

    rejected_connection = SimpleNamespace(adapter_session=None)
    with pytest.raises(PermissionError, match="credential"):
        server._authenticate_adapter(
            rejected_connection,  # type: ignore[arg-type]
            {
                "client_name": "sitg-telegram-bot",
                "channel": "telegram_bot",
                "credential": "wrong-secret",
                "bot_id": 42,
                "environment": "test",
            },
        )


@pytest.mark.asyncio
async def test_remote_gateway_sends_principal_and_typed_request() -> None:
    class CapturingClient:
        action: str | None = None
        params: dict[str, object] | None = None

        async def request(self, action: str, **params: object) -> object:
            self.action = action
            self.params = params
            request = params["request"]
            assert isinstance(request, dict)
            return {
                "contract_version": "1.0",
                "action": request["operation"]["action"],
                "correlation_id": request["metadata"]["correlation_id"],
                "ok": True,
                "data": {"remote": True},
                "error": None,
            }

    client = CapturingClient()
    gateway = RemoteApplicationGateway(client)  # type: ignore[arg-type]
    request = GatewayRequest(
        metadata=RequestMetadata(
            channel="telegram_bot",
            client_name="sitg-telegram-bot",
            client_version="1.0.0",
        ),
        operation=CapabilitiesOperation(action=ActionCode.CAPABILITIES),
    )

    response = await gateway.execute(ApplicationPrincipal(telegram_user_id=123), request)

    assert response.ok
    assert response.data == {"remote": True}
    assert client.action == "application"
    assert client.params is not None
    assert client.params["principal"] == {
        "player_id": None,
        "telegram_user_id": 123,
    }


@pytest.mark.asyncio
async def test_job_worker_records_success_and_terminal_missing_handler() -> None:
    jobs = (
        ClaimedJob(UUID(int=1), UUID(int=11), "known", {"value": 1}, 1),
        ClaimedJob(UUID(int=2), UUID(int=12), "unknown", {}, 1),
    )

    class Queue:
        succeeded: list[tuple[UUID, UUID]] = []
        failed: list[tuple[UUID, UUID, str, bool]] = []

        async def claim(self, *, limit: int) -> tuple[ClaimedJob, ...]:
            assert limit == 20
            return jobs

        async def succeed(self, job_id: UUID, lease_token: UUID) -> None:
            self.succeeded.append((job_id, lease_token))

        async def fail(
            self,
            job_id: UUID,
            lease_token: UUID,
            error: str,
            *,
            terminal: bool = False,
        ) -> None:
            self.failed.append((job_id, lease_token, error, terminal))

    handled: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> None:
        handled.append(payload)

    queue = Queue()
    worker = DurableJobWorker(queue, {"known": handler})  # type: ignore[arg-type]

    assert await worker.run_once() == 2
    assert handled == [{"value": 1}]
    assert queue.succeeded == [(UUID(int=1), UUID(int=11))]
    assert queue.failed[0][0:2] == (UUID(int=2), UUID(int=12))
    assert queue.failed[0][3] is True
