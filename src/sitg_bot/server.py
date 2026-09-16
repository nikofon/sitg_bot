import argparse
import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.bot.miniapps.notifications import TelegramTournamentSelectionNotifier
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.packet_import import packet_from_data
from sitg_bot.services.admin_auth import PlatformAdminAuthenticationService
from sitg_bot.services.launch_references import LaunchReferenceService
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.miniapp_auth import MiniAppAuthService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.persistent_game import ParticipantInput, PersistentGameService, Transition
from sitg_bot.services.reliable_delivery import (
    DurableJobQueue,
    DurableJobWorker,
    TransactionalOutbox,
)
from sitg_bot.services.reliable_scheduling import AutomaticJobScheduler
from sitg_bot.services.ruleset_content import PacketSelection
from sitg_bot.services.telegram_auth import TelegramUpdateAuthService
from sitg_bot.services.token_requests import TournamentTokenRequestService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.services.trust import TrustService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameObserverRecord,
    GameParticipantRecord,
    GameRecord,
    LogicalPacketRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    RulesetRatingRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
    TournamentTypeVersionRecord,
)

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 8 * 1024 * 1024


def json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


@dataclass(slots=True)
class PlayerSession:
    player_id: UUID
    telegram_user_id: int
    public_nickname: str
    admin: bool


@dataclass(frozen=True, slots=True)
class AdapterSession:
    client_name: str
    channel: str
    bot_id: int | None
    environment: str | None


class ClientConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.session: PlayerSession | None = None
        self.adapter_session: AdapterSession | None = None
        self.lobby_ids: set[UUID] = set()
        self.game_ids: set[UUID] = set()
        self._write_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(json_value(message), ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self.writer.write(encoded.encode("utf-8") + b"\n")
            await self.writer.drain()


class ConsoleApplicationServer:
    """Authoritative NDJSON application server for console and presentation adapters."""

    def __init__(
        self,
        database: Database,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        admin_token: str | None = None,
        application_client_token: str | None = None,
        token_delivery_key: str | None = None,
        launch_references: LaunchReferenceService | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if application_client_token is not None and len(application_client_token) < 32:
            raise ValueError("application_client_token must contain at least 32 characters")
        self.database = database
        self.host = host
        self.port = port
        self.admin_token = admin_token
        self.application_client_token = application_client_token
        self.poll_interval = poll_interval
        self.games = PersistentGameService(database)
        self.matchmaking = InvitationMatchmakingService(database)
        self.packet_admin = PacketAdminService(database)
        self.tournaments = TournamentService(database)
        self.trust = TrustService(database)
        self.outbox = TransactionalOutbox(database)
        self.jobs = DurableJobQueue(database)
        self.job_scheduler = AutomaticJobScheduler(database, self.jobs)
        self.job_worker = DurableJobWorker(
            self.jobs,
            {
                "game.deadline": self._job_game_deadline,
                "appeal.deadline": self._job_appeal_deadline,
                "lobby.expire": self._job_lobby_expire,
                "rating.settlement": self._job_rating_settlement,
                "matchmaking.scan": self._job_matchmaking,
                "classic.reconcile": self._job_classic,
                "tournament.start_reminder": self._job_tournament_start_reminder,
                "suspicion.tick": self._job_suspicion,
            },
            poll_interval=poll_interval,
        )
        token_requests = TournamentTokenRequestService(
            database, delivery_encryption_key=token_delivery_key
        )
        self.application_gateway = ApplicationGateway(
            database,
            token_requests=token_requests,
            tournaments=self.tournaments,
            matchmaking=self.matchmaking,
            trust=self.trust,
            packets=self.packet_admin,
            launch_references=launch_references,
            admin_authentication=(
                PlatformAdminAuthenticationService(database, credential=admin_token)
                if admin_token
                else None
            ),
        )
        self._server: asyncio.Server | None = None
        self._connections: set[ClientConnection] = set()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._game_event_sequences: dict[UUID, int] = {}

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("Server has not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            limit=MAX_REQUEST_BYTES,
        )
        self._background_tasks = [
            asyncio.create_task(self._job_scheduler_loop(), name="durable-job-scheduler"),
            asyncio.create_task(self._job_worker_loop(), name="durable-job-worker"),
        ]
        LOGGER.info("Application server listening on %s:%s", self.host, self.bound_port)

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        self._closing = True
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        connections = tuple(self._connections)
        for connection in connections:
            connection.writer.close()
        await asyncio.gather(
            *(connection.writer.wait_closed() for connection in connections),
            return_exceptions=True,
        )
        self._connections.clear()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = ClientConnection(reader, writer)
        self._connections.add(connection)
        peer = writer.get_extra_info("peername")
        LOGGER.info("Console client connected: %s", peer)
        try:
            while not reader.at_eof():
                raw = await reader.readline()
                if not raw:
                    break
                request_id: object = None
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("Request must be a JSON object")
                    request_id = request.get("id")
                    action = request.get("action")
                    params = request.get("params", {})
                    if not isinstance(action, str) or not action:
                        raise ValueError("Request action is required")
                    if not isinstance(params, dict):
                        raise ValueError("Request params must be an object")
                    result = await self._dispatch(connection, action, params)
                    await connection.send({"id": request_id, "ok": True, "result": result})
                except (LookupError, PermissionError, ValueError, TypeError) as error:
                    await connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"type": type(error).__name__, "message": str(error)},
                        }
                    )
                except json.JSONDecodeError as error:
                    await connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"type": "JSONDecodeError", "message": str(error)},
                        }
                    )
                except Exception:
                    LOGGER.exception("Unhandled console request error for action %r", request_id)
                    await connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {
                                "type": "InternalError",
                                "message": "The server could not process the request",
                            },
                        }
                    )
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            LOGGER.info("Console client disconnected unexpectedly: %s", peer)
        finally:
            self._connections.discard(connection)
            writer.close()
            await writer.wait_closed()

    async def _dispatch(
        self, connection: ClientConnection, action: str, params: dict[str, Any]
    ) -> Any:
        if action == "adapter.authenticate":
            return self._authenticate_adapter(connection, params)
        if action == "telegram.update.claim":
            adapter = self._require_adapter(connection, channel="telegram_bot")
            bot_id = self._integer(params, "bot_id", minimum=1)
            if adapter.bot_id != bot_id:
                raise PermissionError("Telegram bot identity does not match adapter session")
            auth = TelegramUpdateAuthService(
                self.database, environment=adapter.environment or "production"
            )
            claim = await auth.claim(
                bot_id=bot_id,
                update_id=self._integer(params, "update_id", minimum=0),
                telegram_user_id=self._integer(params, "telegram_user_id", minimum=1),
                correlation_id=(
                    UUID(str(params["correlation_id"]))
                    if params.get("correlation_id") is not None
                    else None
                ),
            )
            return claim
        if action == "telegram.update.complete":
            adapter = self._require_adapter(connection, channel="telegram_bot")
            auth = TelegramUpdateAuthService(
                self.database, environment=adapter.environment or "production"
            )
            succeeded = params.get("succeeded")
            if not isinstance(succeeded, bool):
                raise ValueError("succeeded must be true or false")
            error_code = params.get("error_code")
            if error_code is not None and not isinstance(error_code, str):
                raise ValueError("error_code must be a string or null")
            await auth.complete(
                self._uuid(params, "receipt_id"),
                succeeded=succeeded,
                error_code=error_code,
            )
            return {"completed": True}
        if action == "telegram.chat.delivery":
            self._require_adapter(connection, channel="telegram_bot")
            return await self.application_gateway.chat.delivery(
                params["payload"], key=params.get("key"), message_id=params.get("message_id")
            )
        if action in {
            "telegram.game.delivery",
            "telegram.game.disconnect",
            "telegram.game.presentation",
            "telegram.game.record",
        }:
            self._require_adapter(connection, channel="telegram_bot")
            games = self.application_gateway.telegram_games
            telegram_user_id = self._integer(params, "telegram_user_id", minimum=1)
            game_id = self._uuid(params, "game_id")
            if action == "telegram.game.presentation":
                return await games.presentation(telegram_user_id, game_id)
            if action == "telegram.game.record":
                key = params.get("key")
                value = params.get("value")
                if key is not None and (
                    not isinstance(key, str) or len(key) > 180 or not isinstance(value, dict)
                ):
                    raise ValueError("Invalid game message record")
                await games.record_delivery(
                    telegram_user_id,
                    game_id,
                    sequence=self._integer(params, "sequence", minimum=0)
                    if "sequence" in params
                    else None,
                    key=key,
                    value=value,
                )
                return {"saved": True}
            if action == "telegram.game.delivery":
                try:
                    return await games.delivery(telegram_user_id, game_id)
                except (PermissionError, LookupError):
                    return {"skip": True}
            if action == "telegram.game.disconnect":
                await games.disconnect(telegram_user_id, game_id)
                return {"saved": True}
        if action == "outbox.claim":
            self._require_adapter(connection)
            topics = params.get("topics", [])
            if not isinstance(topics, list) or not all(
                isinstance(topic, str) and topic for topic in topics
            ):
                raise ValueError("topics must be a list of non-empty strings")
            return await self.outbox.claim(
                topics=tuple(topics),
                limit=self._integer(params, "limit", minimum=1, maximum=500),
            )
        if action == "outbox.acknowledge":
            self._require_adapter(connection)
            await self.outbox.acknowledge(
                self._uuid(params, "event_id"), self._uuid(params, "lease_token")
            )
            return {"acknowledged": True}
        if action == "outbox.reject":
            self._require_adapter(connection)
            terminal = params.get("terminal", False)
            if not isinstance(terminal, bool):
                raise ValueError("terminal must be true or false")
            retry_after_seconds = params.get("retry_after_seconds")
            retry_after = (
                timedelta(
                    seconds=self._integer(
                        params,
                        "retry_after_seconds",
                        minimum=0,
                        maximum=86_400,
                    )
                )
                if retry_after_seconds is not None
                else None
            )
            await self.outbox.reject(
                self._uuid(params, "event_id"),
                self._uuid(params, "lease_token"),
                self._string(params, "error"),
                terminal=terminal,
                retry_after=retry_after,
            )
            return {"rejected": True}
        if action == "application":
            if "request" in params:
                adapter = self._require_adapter(connection)
                request = GatewayRequest.model_validate(params["request"])
                if (
                    request.metadata.channel != adapter.channel
                    or request.metadata.client_name != adapter.client_name
                ):
                    raise PermissionError(
                        "Application request metadata does not match adapter session"
                    )
                principal_data = params.get("principal")
                if not isinstance(principal_data, dict):
                    raise ValueError("Application principal is required")
                player_id = principal_data.get("player_id")
                telegram_user_id = principal_data.get("telegram_user_id")
                parsed_telegram_user_id = (
                    self._integer(
                        principal_data,
                        "telegram_user_id",
                        minimum=1,
                    )
                    if telegram_user_id is not None
                    else None
                )
                principal = ApplicationPrincipal(
                    player_id=UUID(str(player_id)) if player_id is not None else None,
                    telegram_user_id=parsed_telegram_user_id,
                )
            else:
                request = GatewayRequest.model_validate(params)
                session = connection.session
                principal = ApplicationPrincipal(
                    player_id=session.player_id if session else None,
                    telegram_user_id=session.telegram_user_id if session else None,
                )
            response = await self.application_gateway.execute(principal, request)
            return response.model_dump(mode="json")
        return await self._dispatch_console(connection, action, params)

    def _authenticate_adapter(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        if connection.adapter_session is not None:
            raise PermissionError("Adapter connection is already authenticated")
        configured = self.application_client_token
        credential = params.get("credential")
        if (
            configured is None
            or not isinstance(credential, str)
            or not hmac.compare_digest(configured, credential)
        ):
            raise PermissionError("Invalid application adapter credential")
        client_name = self._string(params, "client_name")
        channel = self._string(params, "channel")
        if channel not in {"telegram_bot", "mini_app"}:
            raise ValueError("Unsupported application adapter channel")
        environment_value = params.get("environment")
        bot_id = (
            self._integer(params, "bot_id", minimum=1) if params.get("bot_id") is not None else None
        )
        environment = str(environment_value) if environment_value is not None else None
        if channel == "telegram_bot":
            if bot_id is None or bot_id <= 0:
                raise ValueError("Telegram adapter requires a positive bot_id")
            if environment not in {"production", "test"}:
                raise ValueError("Telegram adapter environment is invalid")
        connection.adapter_session = AdapterSession(
            client_name=client_name,
            channel=channel,
            bot_id=bot_id,
            environment=environment,
        )
        return {"authenticated": True, "channel": channel}

    @staticmethod
    def _require_adapter(
        connection: ClientConnection, *, channel: str | None = None
    ) -> AdapterSession:
        adapter = connection.adapter_session
        if adapter is None or (channel is not None and adapter.channel != channel):
            raise PermissionError("Authenticated application adapter is required")
        return adapter

    async def _dispatch_console(
        self, connection: ClientConnection, action: str, params: dict[str, Any]
    ) -> Any:
        if action == "login":
            return await self._login(connection, params)
        session = self._session(connection)

        if action == "me":
            return await self._context(connection)
        if action == "packets":
            tournament_id = self._uuid(params, "tournament_id")
            return await self._list_packets(session.player_id, tournament_id=tournament_id)
        if action in {"tournaments", "tournaments_mine"}:
            return await self.tournaments.list_for_player(session.player_id)
        if action == "tournaments_public":
            return await self.tournaments.list_public()
        if action == "admin_tournament_token_issue":
            self._require_admin(session)
            intended = params.get("intended_creator_id")
            token = await self.tournaments.issue_creation_token(
                session.player_id,
                intended_creator_id=UUID(str(intended)) if intended else None,
            )
            return {"token": token}
        if action == "admin_tournament_token_revoke":
            self._require_admin(session)
            await self.tournaments.revoke_creation_token(
                self._string(params, "token"), administrator_id=session.player_id
            )
            return {"revoked": True}
        if action == "admin_suspicion_review":
            self._require_admin(session)
            limit = int(params.get("limit", 20))
            return await self.trust.review_player_suspicion(
                session.player_id,
                self._uuid(params, "player_id"),
                limit=limit,
            )
        if action == "admin_suspicion_clear":
            self._require_admin(session)
            return await self.trust.clear_player_suspicion(
                session.player_id,
                self._uuid(params, "player_id"),
                note=self._string(params, "note"),
            )
        if action == "tournament_create":
            mutable = params.get("player_mutable_parameters", [])
            defaults = params.get("default_parameters", {})
            policies = params.get("policies", {})
            if not isinstance(mutable, list) or not all(isinstance(item, str) for item in mutable):
                raise ValueError("player_mutable_parameters must be a list of names")
            if not isinstance(defaults, dict) or not isinstance(policies, dict):
                raise ValueError("Tournament parameters and policies must be objects")
            registration_open = params.get("registration_open", False)
            if not isinstance(registration_open, bool):
                raise ValueError("registration_open must be a boolean")
            authors = params.get("authors", [])
            if not isinstance(authors, list) or not all(isinstance(item, str) for item in authors):
                raise ValueError("authors must be a list of names")
            pricing_plans = params.get("pricing_plans", [])
            if not isinstance(pricing_plans, list):
                raise ValueError("pricing_plans must be a list")
            return await self.tournaments.create_tournament(
                raw_token=self._string(params, "token"),
                creator_id=session.player_id,
                name=self._string(params, "name"),
                slug=self._string(params, "slug"),
                type_key=self._string(params, "type"),
                game_ruleset_key=str(params.get("game_ruleset", "si")),
                default_parameters=defaults,
                player_mutable_parameters=frozenset(mutable),
                policies=policies,
                visibility=str(params.get("visibility", "private")),
                language=str(params.get("language", "und")),
                payment_type=str(params.get("payment_type", "free")),
                pricing_plans=pricing_plans,
                registration_open=registration_open,
                registration_starts_at=self._optional_datetime(params, "registration_starts_at"),
                registration_ends_at=self._optional_datetime(params, "registration_ends_at"),
                starts_at=self._optional_datetime(params, "starts_at"),
                planned_ends_at=self._optional_datetime(params, "planned_ends_at"),
                author_names=tuple(authors),
            )
        if action in {"tournament_info", "tournament_manage"}:
            tournament_id = self._uuid(params, "tournament_id")
            async with self.database.sessions() as db_session:
                membership = await db_session.get(
                    TournamentMembershipRecord,
                    (tournament_id, session.player_id),
                )
                if action == "tournament_manage" or membership is None:
                    await self.tournaments._require_manager(
                        db_session, tournament_id, session.player_id
                    )
                tournament = await db_session.get(TournamentRecord, tournament_id)
                if tournament is None:
                    raise LookupError("Tournament not found")
                return await self._tournament_details(
                    db_session,
                    tournament,
                    membership,
                    player_id=session.player_id,
                )
        if action == "tournament_member_add":
            await self.tournaments.enroll(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "player_id"),
                enrolled_by_id=session.player_id,
            )
            return {"enrolled": True}
        if action == "tournament_invite":
            await self.tournaments.invite(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "player_id"),
                invited_by_id=session.player_id,
            )
            return {"invited": True}
        if action == "tournament_register":
            decision = await self.tournaments.register(
                self._uuid(params, "tournament_id"), session.player_id
            )
            return {
                "registered": decision.accepted,
                "declined": not decision.accepted,
                "membership_status": decision.status,
                "reasons": decision.reasons,
            }
        if action == "tournament_registration_requirement_add":
            requirement_id = await self.tournaments.add_registration_requirement(
                self._uuid(params, "tournament_id"),
                session.player_id,
                kind=self._string(params, "kind"),
                target_id=self._uuid(params, "target_id"),
                failure_message=(
                    str(params["failure_message"]) if params.get("failure_message") else None
                ),
            )
            return {"requirement_id": requirement_id}
        if action == "tournament_registration_requirement_remove":
            tournament_id = self._uuid(params, "tournament_id")
            await self.tournaments.remove_registration_requirement(
                tournament_id,
                self._uuid(params, "requirement_id"),
                manager_id=session.player_id,
            )
            return {"requirement_removed": True}
        if action == "tournament_registration_requirements":
            return await self.tournaments.registration_requirements(
                self._uuid(params, "tournament_id"), viewer_id=session.player_id
            )
        if action == "tournament_registrations":
            management = await self.tournaments.manager_management(
                self._uuid(params, "tournament_id"), session.player_id
            )
            return {"registrations": management.registrations}
        if action == "tournament_registrations_approve_all":
            count = await self.tournaments.approve_all_registrations(
                self._uuid(params, "tournament_id"), manager_id=session.player_id
            )
            return {"approved_count": count}
        if action == "tournament_registration_approve":
            status = await self.tournaments.approve_registration(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "player_id"),
                manager_id=session.player_id,
            )
            return {"approved": True, "membership_status": status}
        if action == "tournament_setup_finalize":
            settings = await self.tournaments.finalize_tournament_setup(
                self._uuid(params, "tournament_id"),
                session.player_id,
                expected_version=self._integer(params, "expected_version", minimum=1),
            )
            return {
                "finalized_at": settings.finalized_at,
                "settings_version": settings.settings_version,
            }
        if action == "tournament_start":
            management = await self.tournaments.start_tournament(
                self._uuid(params, "tournament_id"),
                session.player_id,
                expected_version=self._integer(params, "expected_version", minimum=1),
            )
            return {
                "actual_starts_at": management.tournament.actual_starts_at,
                "settings_version": management.settings_version,
            }
        if action == "tournament_stage_start":
            from sitg_bot.services.classic import ClassicService

            kind = self._string(params, "kind")
            await ClassicService(self.database).mutate(
                self._uuid(params, "tournament_id"),
                session.player_id,
                expected_version=self._integer(params, "expected_version", minimum=1),
                command="start",
                kind=kind,
                values={},
            )
            return {"stage_started": True, "kind": kind}
        if action == "tournament_participants_finalize":
            player_ids = params.get("player_ids")
            if not isinstance(player_ids, list):
                raise ValueError("player_ids must be a list")
            await self.tournaments.finalize_participants(
                self._uuid(params, "tournament_id"),
                {UUID(str(item)) for item in player_ids},
                manager_id=session.player_id,
            )
            return {"participants_finalized": True}
        if action == "tournament_manager_add":
            await self.tournaments.add_manager(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "player_id"),
                granted_by_id=session.player_id,
            )
            return {"manager_added": True}
        if action == "tournament_manager_remove":
            await self.tournaments.remove_manager(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "player_id"),
                removed_by_id=session.player_id,
            )
            return {"manager_removed": True}
        if action == "tournament_policy_update":
            defaults = params.get("default_parameters")
            mutable = params.get("player_mutable_parameters")
            policies = params.get("policies")
            if not isinstance(defaults, dict) or not isinstance(policies, dict):
                raise ValueError("Tournament parameters and policies must be objects")
            if not isinstance(mutable, list) or not all(isinstance(item, str) for item in mutable):
                raise ValueError("player_mutable_parameters must be a list of names")
            return await self.tournaments.update_policy(
                self._uuid(params, "tournament_id"),
                session.player_id,
                default_parameters=defaults,
                player_mutable_parameters=frozenset(mutable),
                policies=policies,
            )
        if action == "tournament_metadata_update":
            metadata: dict[str, Any] = {}
            for name in ("language", "payment_type", "registration_open"):
                if name in params:
                    metadata[name] = params[name]
            if "description" in params:
                description = params["description"]
                if not isinstance(description, str):
                    raise ValueError("description must be a string")
                metadata["description"] = description
            if "pricing_plans" in params:
                pricing_plans = params["pricing_plans"]
                if not isinstance(pricing_plans, list):
                    raise ValueError("pricing_plans must be a list")
                metadata["pricing_plans"] = pricing_plans
            for name in (
                "registration_starts_at",
                "registration_ends_at",
                "starts_at",
                "planned_ends_at",
            ):
                if name in params:
                    metadata[name] = self._optional_datetime(params, name)
            if "authors" in params:
                authors = params["authors"]
                if not isinstance(authors, list) or not all(
                    isinstance(item, str) for item in authors
                ):
                    raise ValueError("authors must be a list of names")
                metadata["author_names"] = tuple(authors)
            await self.tournaments.update_metadata(
                self._uuid(params, "tournament_id"), session.player_id, **metadata
            )
            return {"metadata_updated": True}
        if action == "tournament_complete":
            await self.tournaments.complete_tournament(
                self._uuid(params, "tournament_id"),
                session.player_id,
                actual_ends_at=self._optional_datetime(params, "actual_ends_at"),
            )
            return {"completed": True}
        if action == "tournament_packet_assign":
            assignment_id = await self.tournaments.assign_packet(
                self._uuid(params, "tournament_id"),
                self._uuid(params, "packet_id"),
                session.player_id,
                adopted_version_id=(
                    self._uuid(params, "packet_version_id")
                    if params.get("packet_version_id")
                    else None
                ),
                discoverable=bool(params.get("discoverable", False)),
                playable=bool(params.get("playable", False)),
                content_visible=bool(params.get("content_visible", False)),
                editable=bool(params.get("editable", False)),
                access_level=(str(params["access_level"]) if params.get("access_level") else None),
            )
            return {"assignment_id": assignment_id}
        if action == "tournament_packet_entitlement_set":
            rights = params.get("rights")
            if not isinstance(rights, dict) or not all(
                isinstance(key, str) and isinstance(value, bool) for key, value in rights.items()
            ):
                raise ValueError("rights must be an object of boolean entitlements")
            await self.tournaments.set_packet_entitlement(
                self._uuid(params, "assignment_id"),
                self._uuid(params, "player_id"),
                session.player_id,
                access_level=(str(params["access_level"]) if params.get("access_level") else None),
                **rights,
            )
            return {"entitlements_updated": True}
        if action == "packet_library_release_set":
            await self.packet_admin.set_library_release(
                self._uuid(params, "packet_id"),
                session.player_id,
                released=bool(params.get("released", False)),
                version_id=(
                    self._uuid(params, "packet_version_id")
                    if params.get("packet_version_id")
                    else None
                ),
            )
            return {"library_release_updated": True}
        if action == "lobby_create":
            lobby = await self.matchmaking.create_lobby(
                self._participant(session),
                max_players=int(params.get("max_players", 4)),
                tournament_id=self._uuid(params, "tournament_id"),
            )
            connection.lobby_ids.add(lobby.id)
            return lobby
        if action == "lobby_join":
            target = self._string(params, "target")
            try:
                lobby_id = UUID(target)
            except ValueError:
                invitation_code = target
            else:
                existing = await self.matchmaking.get(lobby_id)
                invitation_code = existing.invitation_code
            lobby = await self.matchmaking.join(invitation_code, self._participant(session))
            connection.lobby_ids.add(lobby.id)
            await self._broadcast_lobby(lobby.id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_observe":
            target = self._string(params, "target")
            try:
                lobby_id = UUID(target)
            except ValueError:
                invitation_code = target
            else:
                existing = await self.matchmaking.get(lobby_id)
                invitation_code = existing.invitation_code
            lobby = await self.matchmaking.join(
                invitation_code,
                self._participant(session),
                role="observer",
                confirm_fresh=self._boolean(params, "confirm_fresh", default=False),
            )
            connection.lobby_ids.add(lobby.id)
            await self._broadcast_lobby(lobby.id, exclude=connection)
            return lobby
        if action == "lobby_info":
            lobby_id = self._uuid(params, "lobby_id")
            await self._require_lobby_member(lobby_id, session.player_id)
            connection.lobby_ids.add(lobby_id)
            return await self.matchmaking.get(lobby_id)
        if action == "lobby_packet":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.select_packet(
                lobby_id, session.telegram_user_id, self._uuid(params, "packet_id")
            )
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_packet_remove":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.remove_packet(
                lobby_id, session.telegram_user_id, self._uuid(params, "packet_id")
            )
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_settings":
            lobby_id = self._uuid(params, "lobby_id")
            changes = params.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("changes must be a non-empty object")
            lobby = await self.matchmaking.set_settings(lobby_id, session.telegram_user_id, changes)
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_ready":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.set_ready(
                lobby_id,
                session.telegram_user_id,
                ready=self._boolean(params, "ready", default=True),
            )
            await self._broadcast_lobby(lobby_id, exclude=connection)
            return lobby
        if action == "lobby_role":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.set_role(
                lobby_id,
                session.telegram_user_id,
                self._string(params, "role"),
                confirm_fresh=self._boolean(params, "confirm_fresh", default=False),
            )
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_leave":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.leave(lobby_id, session.telegram_user_id)
            connection.lobby_ids.discard(lobby_id)
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_cancel":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.cancel(lobby_id, session.telegram_user_id)
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby
        if action == "lobby_start":
            lobby_id = self._uuid(params, "lobby_id")
            result = await self.matchmaking.start(lobby_id, session.telegram_user_id)
            await self._broadcast_lobby(lobby_id, exclude=connection)
            if result.game is not None:
                await self._subscribe_game_members(result.game.id)
                await self._broadcast_game(result.game.id, snapshot=result.game)
            return result
        if action == "lobby_suggestions":
            lobby_id = self._uuid(params, "lobby_id")
            await self._require_lobby_member(lobby_id, session.player_id)
            return await self.matchmaking.suggest_packets(lobby_id)
        if action == "lobby_find_players":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.find_players(lobby_id, session.telegram_user_id)
            connection.lobby_ids.add(lobby.id)
            await self._run_matchmaking()
            active_lobby_id = await self.matchmaking.active_lobby_id(session.player_id)
            if active_lobby_id is None:
                raise RuntimeError("Matchmaking removed the player's active lobby")
            connection.lobby_ids.add(active_lobby_id)
            await self._broadcast_lobby(active_lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return await self.matchmaking.get(active_lobby_id)
        if action == "lobby_find_players_cancel":
            lobby_id = self._uuid(params, "lobby_id")
            lobby = await self.matchmaking.cancel_search(lobby_id, session.telegram_user_id)
            await self._broadcast_lobby(lobby_id, exclude=connection)
            await self._broadcast_search_pool()
            return lobby

        if action == "blacklist_add":
            return await self.matchmaking.blacklist_player(
                session.player_id, int(params.get("telegram_user_id"))
            )
        if action == "blacklist_remove":
            return await self.matchmaking.unblacklist_player(
                session.player_id, int(params.get("telegram_user_id"))
            )
        if action == "blacklist_list":
            return await self.matchmaking.blacklist(session.player_id)

        if action == "manager_appeal_tickets":
            tournament_id = (
                self._uuid(params, "tournament_id")
                if params.get("tournament_id") is not None
                else None
            )
            return await self.games.manager_appeal_tickets(
                session.player_id, tournament_id=tournament_id
            )
        if action == "manager_appeal_decide":
            appeal_id = self._uuid(params, "appeal_id")
            transition = await self.games.decide_escalated_appeal(
                appeal_id,
                session.player_id,
                approve=self._boolean(params, "approve", default=False),
            )
            await self._broadcast_transition(transition.snapshot.id, transition)
            resolution = next(
                (
                    event["payload"]
                    for event in transition.events
                    if event["kind"] == "appeal_resolved"
                ),
                {"appeal_id": str(appeal_id), "approved": False, "source": "timeout"},
            )
            return {"closed": True, **resolution}

        if action == "game_observe_list":
            return await self.games.observable_games(
                self._uuid(params, "tournament_id"), session.player_id
            )
        if action == "game_observe":
            game_id = self._uuid(params, "game_id")
            result = await self.games.observe(
                game_id,
                session.telegram_user_id,
                confirm_fresh=self._boolean(params, "confirm_fresh", default=False),
            )
            if result.joined:
                connection.game_ids.add(game_id)
            return result
        if action == "game_observe_leave":
            game_id = self._uuid(params, "game_id")
            snapshot = await self.games.stop_observing(game_id, session.telegram_user_id)
            connection.game_ids.discard(game_id)
            return snapshot

        if action == "game_reputation_vote":
            raw_value = params.get("value")
            if isinstance(raw_value, bool):
                raise ValueError("Reputation vote must be an upvote or downvote")
            return await self.trust.vote_reputation(
                self._uuid(params, "game_id"),
                session.player_id,
                int(params.get("telegram_user_id")),
                int(raw_value),
            )
        if action == "game_report":
            details = params.get("details")
            if details is not None and not isinstance(details, str):
                raise ValueError("Report details must be text")
            return await self.trust.report_player(
                self._uuid(params, "game_id"),
                session.player_id,
                int(params.get("telegram_user_id")),
                self._string(params, "kind"),
                details=details,
            )

        if action.startswith("game_"):
            game_id = self._uuid(params, "game_id")
            game_role = await self._game_role(game_id, session.player_id)
            if game_role == "observer" and action not in {"game_info", "game_score"}:
                raise PermissionError("Observers cannot alter game state")
            if action != "game_join":
                connection.game_ids.add(game_id)
            if action == "game_info":
                return await self.games.recover(game_id)
            if action == "game_join":
                observed_game_ids = await self.games.active_observed_game_ids(session.player_id)
                transition = await self.games.join(game_id, session.telegram_user_id)
                connection.game_ids.difference_update(observed_game_ids)
            elif action == "game_buzz":
                transition = await self.games.buzz(game_id, session.telegram_user_id)
                if transition.accepted:
                    await self._broadcast(
                        self._game_subscribers(game_id, exclude=connection),
                        {
                            "event": "player_buzzed",
                            "game_id": game_id,
                            "player": {
                                "telegram_user_id": session.telegram_user_id,
                                "display_name": session.public_nickname,
                            },
                        },
                    )
            elif action == "game_answer":
                answer = self._string(params, "answer")
                transition = await self.games.answer(game_id, session.telegram_user_id, answer)
                if transition.accepted:
                    await self._broadcast(
                        self._game_subscribers(game_id, exclude=connection),
                        {
                            "event": "player_answered",
                            "game_id": game_id,
                            "player": {
                                "telegram_user_id": session.telegram_user_id,
                                "display_name": session.public_nickname,
                            },
                            "answer": answer,
                        },
                    )
            elif action == "game_pause":
                transition = await self.games.pause(game_id, session.telegram_user_id)
            elif action == "game_resume":
                transition = await self.games.resume(game_id, session.telegram_user_id)
            elif action == "game_score":
                transition = (
                    await self.games.request_observer_score(game_id, session.telegram_user_id)
                    if game_role == "observer"
                    else await self.games.request_score(game_id, session.telegram_user_id)
                )
            elif action == "game_appeal":
                connected_player_ids = {
                    item.session.telegram_user_id
                    for item in self._connections
                    if item.session is not None and game_id in item.game_ids
                }
                target = params.get("target_attempt_id")
                transition = await self.games.submit_appeal(
                    game_id,
                    session.telegram_user_id,
                    connected_player_ids,
                    target_attempt_id=UUID(str(target)) if target is not None else None,
                )
            elif action == "game_appeal_vote":
                transition = await self.games.vote_appeal(
                    game_id,
                    self._uuid(params, "appeal_id"),
                    session.telegram_user_id,
                    approve=self._boolean(params, "approve", default=False),
                )
            elif action == "game_appeal_escalate":
                transition = await self.games.choose_appeal_escalation(
                    game_id,
                    self._uuid(params, "appeal_id"),
                    session.telegram_user_id,
                    escalate=self._boolean(params, "escalate", default=False),
                )
            elif action == "game_appeal_commentary":
                transition = await self.games.submit_appeal_commentary(
                    game_id,
                    self._uuid(params, "appeal_id"),
                    session.telegram_user_id,
                    self._string(params, "commentary"),
                )
            elif action == "game_abandon":
                transition = await self.games.abandon_player(game_id, session.telegram_user_id)
            elif action == "game_advance":
                transition = await self.games.advance(game_id)
            else:
                raise ValueError(f"Unknown action: {action}")
            if action == "game_join":
                connection.game_ids.add(game_id)
            if game_role == "observer" and action == "game_score":
                return transition
            await self._broadcast_transition(game_id, transition, exclude=connection)
            if action == "game_abandon":
                connection.game_ids.discard(game_id)
            return transition

        if action == "packet_import":
            try:
                source = base64.b64decode(self._string(params, "source_base64"), validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("Packet upload is not valid base64") from error
            return await self.packet_admin.import_upload(
                source,
                source_filename=self._string(params, "source_filename"),
                uploader_id=session.player_id,
                tournament_id=self._uuid(params, "tournament_id"),
            )
        if action == "packet_preview":
            return await self.packet_admin.editable_draft(
                self._uuid(params, "draft_id"), session.player_id
            )
        if action in {"packet_publish", "packet_reject"}:
            draft_id = self._uuid(params, "draft_id")
            summary = await self.packet_admin.draft_summary(draft_id, session.player_id)
            if summary["status"] in {"published", "rejected"}:
                return summary
            if action == "packet_reject":
                await self.packet_admin.reject(
                    draft_id, actor_id=session.player_id, notify_bound_telegram=True
                )
                return await self.packet_admin.draft_summary(draft_id, session.player_id)
            packet = await self.packet_admin.publish(
                draft_id, administrator_id=session.player_id, notify_bound_telegram=True
            )
            return {
                "draft_id": draft_id,
                "status": "published",
                "packet_id": packet.logical_id,
                "packet_version_id": packet.version_id,
                "name": packet.packet.name,
            }
        if action == "admin_packet_import":
            self._require_admin(session)
            content = params.get("content")
            if not isinstance(content, dict):
                raise ValueError("Packet content must be a JSON object")
            packet = packet_from_data(content)
            draft_id = await self.packet_admin.create_draft(
                packet,
                source_filename=self._string(params, "source_filename"),
                uploader_id=session.player_id,
                tournament_id=self._uuid(params, "tournament_id"),
            )
            return {"draft_id": draft_id}
        if action == "admin_packet_preview":
            self._require_admin(session)
            return await self.packet_admin.preview(self._uuid(params, "draft_id"))
        if action == "admin_packet_publish":
            self._require_admin(session)
            packet = await self.packet_admin.publish(
                self._uuid(params, "draft_id"), administrator_id=session.player_id
            )
            return {
                "packet_id": packet.logical_id,
                "packet_version_id": packet.version_id,
                "name": packet.packet.name,
            }
        if action == "admin_packet_reject":
            self._require_admin(session)
            await self.packet_admin.reject(self._uuid(params, "draft_id"))
            return {"rejected": True}
        raise ValueError(f"Unknown action: {action}")

    async def _login(self, connection: ClientConnection, params: dict[str, Any]) -> dict[str, Any]:
        telegram_user_id = int(params.get("telegram_user_id"))
        display_name = self._string(params, "display_name").strip()
        if not display_name:
            raise ValueError("Display name cannot be empty")
        wants_admin = bool(params.get("admin", False))
        if (
            wants_admin
            and self.admin_token is not None
            and params.get("admin_token") != self.admin_token
        ):
            raise PermissionError("Invalid administrator token")
        async with self.database.transaction() as db_session:
            player = await db_session.scalar(
                select(PlayerRecord)
                .where(PlayerRecord.telegram_user_id == telegram_user_id)
                .with_for_update()
            )
            if player is None:
                now = datetime.now(UTC)
                player = PlayerRecord(
                    telegram_user_id=telegram_user_id,
                    real_name=display_name,
                    public_nickname=display_name,
                    registration_step="complete",
                    registration_completed_at=now,
                    status="active",
                )
                db_session.add(player)
                await db_session.flush()
            elif player.status != "active":
                raise PermissionError("Player account is not active")
            elif player.public_nickname is None:
                raise PermissionError("Player account has no public nickname")
            if wants_admin:
                administrator = await db_session.get(PlatformAdministratorRecord, player.id)
                if administrator is None:
                    db_session.add(PlatformAdministratorRecord(player_id=player.id))
                else:
                    administrator.revoked_at = None
            assert player.public_nickname is not None
            session = PlayerSession(
                player.id, telegram_user_id, player.public_nickname, wants_admin
            )
        connection.session = session
        connection.lobby_ids.clear()
        connection.game_ids.clear()
        context = await self._context(connection)
        return {"player": session, **context}

    async def _context(self, connection: ClientConnection) -> dict[str, Any]:
        session = self._session(connection)
        async with self.database.sessions() as db_session:
            player = await db_session.get(PlayerRecord, session.player_id)
            assert player is not None
            lobby_id = await db_session.scalar(
                select(PregameLobbyMemberRecord.lobby_id)
                .where(
                    PregameLobbyMemberRecord.player_id == player.id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
                .limit(1)
            )
            game_id = await db_session.scalar(
                select(GameParticipantRecord.game_id)
                .where(
                    GameParticipantRecord.player_id == player.id,
                    GameParticipantRecord.active.is_(True),
                )
                .limit(1)
            )
            reconnectable_game_id = await db_session.scalar(
                select(GameParticipantRecord.game_id)
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .where(
                    GameParticipantRecord.player_id == player.id,
                    GameParticipantRecord.active.is_(False),
                    GameParticipantRecord.abandoned_at.is_not(None),
                    GameRecord.status == "active",
                )
                .order_by(GameParticipantRecord.abandoned_at.desc())
                .limit(1)
            )
            last_game_id = await db_session.scalar(
                select(GameParticipantRecord.game_id)
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .where(GameParticipantRecord.player_id == player.id)
                .order_by(GameRecord.created_at.desc())
                .limit(1)
            )
            observed_game_ids = tuple(
                (
                    await db_session.execute(
                        select(GameObserverRecord.game_id)
                        .join(GameRecord, GameRecord.id == GameObserverRecord.game_id)
                        .where(
                            GameObserverRecord.player_id == player.id,
                            GameObserverRecord.active.is_(True),
                            GameRecord.status.in_(("lobby", "active")),
                        )
                        .order_by(GameObserverRecord.joined_at)
                    )
                ).scalars()
            )
            result: dict[str, Any] = {
                "player_id": player.id,
                "telegram_user_id": player.telegram_user_id,
                "display_name": player.public_nickname,
                "reputation": player.reputation,
                "suspicion": player.suspicion,
                "admin": session.admin,
                "lobby_id": lobby_id,
                "game_id": game_id,
                "reconnectable_game_id": reconnectable_game_id,
                "last_game_id": last_game_id,
                "observed_game_ids": observed_game_ids,
            }
        if lobby_id is not None:
            connection.lobby_ids.add(lobby_id)
        if game_id is not None:
            connection.game_ids.add(game_id)
        elif reconnectable_game_id is not None:
            snapshot = await self.games.recover(reconnectable_game_id)
            reconnectable = next(
                (
                    participant.can_reconnect
                    for participant in snapshot.participants
                    if participant.telegram_user_id == session.telegram_user_id
                ),
                False,
            )
            if not reconnectable:
                result["reconnectable_game_id"] = None
        connection.game_ids.update(observed_game_ids)
        return result

    async def _list_packets(self, player_id: UUID, *, tournament_id: UUID) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            context = await self.tournaments.context(session, tournament_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            content_adapter = self.matchmaking.content_adapters.get(
                context.ruleset_key, context.ruleset_version
            )
            rows = list(
                (
                    await session.execute(
                        select(LogicalPacketRecord, TournamentPacketAssignmentRecord)
                        .join(
                            TournamentPacketAssignmentRecord,
                            TournamentPacketAssignmentRecord.packet_id == LogicalPacketRecord.id,
                        )
                        .where(
                            LogicalPacketRecord.retired_at.is_(None),
                            TournamentPacketAssignmentRecord.tournament_id == context.tournament_id,
                            TournamentPacketAssignmentRecord.status == "active",
                        )
                        .order_by(LogicalPacketRecord.created_at)
                    )
                ).all()
            )
            result: list[dict[str, Any]] = []
            for packet, assignment in rows:
                if not await self.tournaments.has_assignment_access(
                    session, assignment, player_id, "discoverable"
                ):
                    continue
                version = (
                    await session.get(PacketVersionRecord, assignment.adopted_version_id)
                    if assignment.adopted_version_id is not None
                    else await session.scalar(
                        select(PacketVersionRecord)
                        .where(
                            PacketVersionRecord.packet_id == packet.id,
                            PacketVersionRecord.state == "published",
                        )
                        .order_by(PacketVersionRecord.version_number.desc())
                        .limit(1)
                    )
                )
                if version is None:
                    continue
                available = await content_adapter.available_play_units(
                    session,
                    [PacketSelection(version.id, 1)],
                    [player_id],
                )
                total_play_unit_count = await content_adapter.play_unit_count(session, version.id)
                has_playable_access = await self.tournaments.has_assignment_access(
                    session, assignment, player_id, "playable"
                )
                violations = [
                    {"code": violation.code, "details": dict(violation.details)}
                    for violation in ruleset.validate_lobby(
                        player_count=1,
                        packet_count=1,
                        available_play_units=available,
                        parameters=context.settings,
                    )
                ]
                if not has_playable_access:
                    violations.append(
                        {"code": "packet_not_playable", "details": {"reason": "entitlement"}}
                    )
                result.append(
                    {
                        "packet_id": packet.id,
                        "packet_version_id": version.id,
                        "name": version.name,
                        "year": version.year,
                        "language": version.language,
                        "library_released_at": version.library_released_at,
                        "lead_author": (
                            await session.scalar(
                                select(AuthorRecord.display_name).where(
                                    AuthorRecord.id == version.lead_author_id
                                )
                            )
                            if version.lead_author_id is not None
                            else None
                        ),
                        "version": version.version_number,
                        "playability": {
                            "playable": not violations,
                            "violations": violations,
                        },
                        "access": {
                            "level": await self.tournaments.packet_access_level(
                                session, assignment, player_id
                            ),
                            "playable": has_playable_access,
                            "content_visible": await self.tournaments.has_assignment_access(
                                session, assignment, player_id, "content_visible"
                            ),
                            "editable": await self.tournaments.has_assignment_access(
                                session, assignment, player_id, "editable"
                            ),
                        },
                        "total_play_unit_count": total_play_unit_count,
                        "fresh_play_unit_count": len(available),
                    }
                )
            return result

    async def _tournament_details(
        self,
        db_session: Any,
        tournament: TournamentRecord,
        membership: TournamentMembershipRecord | None,
        *,
        player_id: UUID,
    ) -> dict[str, Any]:
        context = await self.tournaments.context(db_session, tournament.id)
        type_version = await db_session.get(TournamentTypeVersionRecord, tournament.type_version_id)
        manager = await db_session.get(TournamentManagerRecord, (tournament.id, player_id))
        ruleset_rating = await db_session.get(RulesetRatingRecord, (context.ruleset_key, player_id))
        now = datetime.now(UTC)
        tournament_confidence = self.games.rating_confidence.calculate(
            current_rating=Decimal(membership.rating) if membership is not None else Decimal(1000),
            history=await self.games._tournament_rating_history(
                db_session, tournament.id, player_id
            ),
            as_of=now,
        )
        ruleset_rating_value = (
            Decimal(ruleset_rating.rating) if ruleset_rating is not None else Decimal(1000)
        )
        ruleset_confidence = self.games.rating_confidence.calculate(
            current_rating=ruleset_rating_value,
            history=await self.games._ruleset_rating_history(
                db_session, context.ruleset_key, player_id
            ),
            as_of=now,
        )
        assert type_version is not None
        return {
            "id": tournament.id,
            "name": tournament.name,
            "slug": tournament.slug,
            "description": tournament.description,
            "status": tournament.status,
            "visibility": tournament.visibility,
            "language": tournament.language,
            "payment_type": tournament.payment_type,
            "pricing_plans": await self.tournaments.pricing_plans(db_session, tournament.id),
            "authors": await self.tournaments.tournament_authors(db_session, tournament.id),
            "registration_open": self.tournaments._registration_is_open(tournament, now),
            "registration_starts_at": tournament.registration_starts_at,
            "registration_ends_at": tournament.registration_ends_at,
            "starts_at": tournament.starts_at,
            "actual_starts_at": tournament.actual_starts_at,
            "planned_ends_at": tournament.planned_ends_at,
            "actual_ends_at": tournament.actual_ends_at,
            "participants_finalized_at": tournament.participants_finalized_at,
            "finalized_at": tournament.finalized_at,
            "settings_version": tournament.settings_version,
            "type": context.type_key,
            "type_version": type_version.version,
            "game_ruleset": context.ruleset_key,
            "game_ruleset_version": context.ruleset_version,
            "policy_version": context.policy_version,
            "default_parameters": context.settings,
            "player_mutable_parameters": sorted(context.mutable_parameters),
            "policies": context.policies,
            "type_supports_hybrid_matchmaking": context.type_supports_hybrid_matchmaking,
            "hybrid_matchmaking_enabled": context.hybrid_matchmaking_enabled,
            "membership_status": membership.status if membership is not None else None,
            "registration_rejected_at": (
                membership.registration_rejected_at if membership is not None else None
            ),
            "registration_rejection_reasons": (
                membership.registration_rejection_reasons if membership is not None else []
            ),
            "registration_requirements": await self.tournaments.registration_requirements(
                tournament.id
            ),
            "rating": membership.rating if membership is not None else None,
            "rating_confidence": (
                tournament_confidence.confidence if membership is not None else None
            ),
            "rating_sequence": membership.rating_sequence if membership is not None else None,
            "ruleset_rating": ruleset_rating_value,
            "ruleset_rating_confidence": ruleset_confidence.confidence,
            "rating_confidence_model": self.games.rating_confidence.key,
            "manager": manager is not None and manager.revoked_at is None,
        }

    async def _progression_loop(self) -> None:
        while not self._closing:
            try:
                for game_id in await self.games.terminate_unattended_games():
                    await self._broadcast_game(game_id)
                pause_deadline_game_ids = await self.games.sync_pause_abandonment_deadlines(
                    await self._connected_player_ids_by_game()
                )
                for game_id in pause_deadline_game_ids:
                    await self._broadcast_game(game_id)
                game_ids = await self.games.progress_all_due()
                for game_id in game_ids:
                    await self._broadcast_game(game_id)
                appeal_game_ids = await self.games.progress_due_appeals()
                for game_id in set(appeal_game_ids):
                    await self._broadcast_game(game_id)
                settled_game_ids = await self.games.settle_pending_ratings()
                for game_id in settled_game_ids:
                    await self._broadcast_game(game_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Automatic game progression failed; polling will continue")
            await asyncio.sleep(self.poll_interval)

    async def _job_scheduler_loop(self) -> None:
        while not self._closing:
            try:
                for game_id in await self.games.terminate_unattended_games():
                    await self._broadcast_game(game_id)
                changed_game_ids = await self.games.sync_pause_abandonment_deadlines(
                    await self._connected_player_ids_by_game()
                )
                for game_id in changed_game_ids:
                    await self._broadcast_game(game_id)
                await self.job_scheduler.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Durable job reconciliation failed; polling will continue")
            await asyncio.sleep(self.poll_interval)

    async def _job_worker_loop(self) -> None:
        while not self._closing:
            try:
                processed = await self.job_worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Durable job worker failed; polling will continue")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self.poll_interval)

    async def _job_game_deadline(self, payload: dict[str, Any]) -> None:
        game_id = UUID(str(payload["game_id"]))
        transition = await self.games.progress_due(game_id)
        if transition.accepted:
            await self._broadcast_game(game_id)

    async def _job_appeal_deadline(self, payload: dict[str, Any]) -> None:
        game_id = UUID(str(payload["game_id"]))
        progressed = await self.games.progress_due_appeals()
        if game_id in progressed:
            await self._broadcast_game(game_id)

    async def _job_lobby_expire(self, payload: dict[str, Any]) -> None:
        expected_lobby_id = UUID(str(payload["lobby_id"]))
        lobby_ids = await self.matchmaking.expire_due()
        for lobby_id in lobby_ids:
            await self._broadcast_lobby(lobby_id)
        if expected_lobby_id in lobby_ids:
            await self._broadcast_search_pool()

    async def _job_rating_settlement(self, payload: dict[str, Any]) -> None:
        del payload
        for game_id in await self.games.settle_pending_ratings():
            await self._broadcast_game(game_id)

    async def _job_tournament_start_reminder(self, payload: dict[str, Any]) -> None:
        await self.tournaments.remind_scheduled_starts()

    async def _job_classic(self, payload: dict[str, Any]) -> None:
        from sitg_bot.services.classic import ClassicService

        await ClassicService(self.database).reconcile()

    async def _job_matchmaking(self, payload: dict[str, Any]) -> None:
        del payload
        await self._run_matchmaking()

    async def _job_suspicion(self, payload: dict[str, Any]) -> None:
        del payload
        summary = await self.trust.process_suspicion_tick()
        if any(
            (
                summary.materialized_games,
                summary.enqueued_players,
                summary.claimed_jobs,
                summary.raised_points,
                summary.failed_players,
            )
        ):
            LOGGER.info("SI suspicion durable job completed: %s", summary)

    async def _connected_player_ids_by_game(self) -> dict[UUID, set[UUID]]:
        connected: dict[UUID, set[UUID]] = {}
        for connection in self._connections:
            if connection.session is None:
                continue
            for game_id in connection.game_ids:
                connected.setdefault(game_id, set()).add(connection.session.player_id)
        if any(
            c.adapter_session and c.adapter_session.channel == "telegram_bot"
            for c in self._connections
        ):
            from sitg_bot.storage.models import TelegramGameViewRecord

            async with self.database.sessions() as session:
                rows = (
                    await session.execute(
                        select(TelegramGameViewRecord.game_id, TelegramGameViewRecord.player_id)
                        .join(
                            GameParticipantRecord,
                            (
                                (GameParticipantRecord.game_id == TelegramGameViewRecord.game_id)
                                & (
                                    GameParticipantRecord.player_id
                                    == TelegramGameViewRecord.player_id
                                )
                            ),
                        )
                        .where(
                            TelegramGameViewRecord.connected.is_(True),
                            GameParticipantRecord.active.is_(True),
                        )
                    )
                ).all()
                for game_id, player_id in rows:
                    connected.setdefault(game_id, set()).add(player_id)
        return connected

    async def _expiration_loop(self) -> None:
        while not self._closing:
            try:
                lobby_ids = await self.matchmaking.expire_due()
                for lobby_id in lobby_ids:
                    await self._broadcast_lobby(lobby_id)
                if lobby_ids:
                    await self._broadcast_search_pool()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Lobby expiration failed; polling will continue")
            await asyncio.sleep(max(self.poll_interval, 1.0))

    async def _matchmaking_loop(self) -> None:
        while not self._closing:
            try:
                await self._run_matchmaking()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Lobby matchmaking failed; polling will continue")
            await asyncio.sleep(max(self.poll_interval, 0.25))

    async def _suspicion_loop(self) -> None:
        while not self._closing:
            try:
                summary = await self.trust.process_suspicion_tick()
                if any(
                    (
                        summary.materialized_games,
                        summary.enqueued_players,
                        summary.claimed_jobs,
                        summary.raised_points,
                        summary.failed_players,
                    )
                ):
                    LOGGER.info("SI suspicion processing tick completed: %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Suspicion evaluation failed; polling will continue")
            await asyncio.sleep(30)

    async def _run_matchmaking(self) -> None:
        merges = await self.matchmaking.match_searching()
        for merge in merges:
            for connection in self._connections:
                if merge.discarded_lobby_id in connection.lobby_ids:
                    connection.lobby_ids.discard(merge.discarded_lobby_id)
                    connection.lobby_ids.add(merge.surviving_lobby_id)
            await self._broadcast_lobby(merge.surviving_lobby_id)
        if merges:
            await self._broadcast_search_pool()

    async def _broadcast_search_pool(self) -> None:
        for lobby_id in await self.matchmaking.searching_lobby_ids():
            await self._broadcast_lobby(lobby_id)

    async def _broadcast_transition(
        self,
        game_id: UUID,
        transition: Transition,
        *,
        exclude: ClientConnection | None = None,
    ) -> None:
        if transition.events:
            self._game_event_sequences[game_id] = max(
                int(event["sequence"]) for event in transition.events
            )
        await self._broadcast_game(
            game_id,
            snapshot=transition.snapshot,
            events=transition.events,
            exclude=exclude,
        )

    async def _broadcast_game(
        self,
        game_id: UUID,
        *,
        snapshot: Any | None = None,
        events: Any | None = None,
        exclude: ClientConnection | None = None,
    ) -> None:
        snapshot = snapshot or await self.games.recover(game_id)
        if events is None:
            all_events = await self.games.events(game_id)
            previous = self._game_event_sequences.get(game_id, 0)
            events = tuple(event for event in all_events if int(event["sequence"]) > previous)
            if events:
                self._game_event_sequences[game_id] = max(
                    int(event["sequence"]) for event in events
                )
        await self._broadcast(
            self._game_subscribers(game_id, exclude=exclude),
            {
                "event": "game_changed",
                "game_id": game_id,
                "snapshot": snapshot,
                "events": events,
            },
        )

    async def _broadcast_lobby(
        self, lobby_id: UUID, *, exclude: ClientConnection | None = None
    ) -> None:
        snapshot = await self.matchmaking.get(lobby_id)
        await self._broadcast(
            (
                connection
                for connection in self._connections
                if lobby_id in connection.lobby_ids and connection is not exclude
            ),
            {"event": "lobby_changed", "lobby_id": lobby_id, "snapshot": snapshot},
        )

    async def _subscribe_game_members(self, game_id: UUID) -> None:
        snapshot = await self.games.recover(game_id)
        player_ids = {
            participant.telegram_user_id
            for participant in snapshot.participants
            if participant.telegram_user_id is not None
        }
        player_ids.update(
            observer.telegram_user_id
            for observer in snapshot.observers
            if observer.telegram_user_id is not None and observer.active
        )
        for connection in self._connections:
            if connection.session and connection.session.telegram_user_id in player_ids:
                connection.game_ids.add(game_id)

    def _game_subscribers(self, game_id: UUID, *, exclude: ClientConnection | None = None) -> Any:
        return (
            connection
            for connection in self._connections
            if game_id in connection.game_ids and connection is not exclude
        )

    async def _broadcast(self, connections: Any, message: dict[str, Any]) -> None:
        recipients = tuple(connections)
        results = await asyncio.gather(
            *(connection.send(message) for connection in recipients), return_exceptions=True
        )
        for connection, result in zip(recipients, results, strict=True):
            if isinstance(result, Exception):
                LOGGER.info("Dropping unavailable console client: %s", result)
                self._connections.discard(connection)
                connection.writer.close()

    async def _require_lobby_member(self, lobby_id: UUID, player_id: UUID) -> None:
        async with self.database.sessions() as session:
            member = await session.scalar(
                select(PregameLobbyMemberRecord.id).where(
                    PregameLobbyMemberRecord.lobby_id == lobby_id,
                    PregameLobbyMemberRecord.player_id == player_id,
                )
            )
            if member is None:
                raise PermissionError("Player is not a lobby member")

    async def _game_role(self, game_id: UUID, player_id: UUID) -> str:
        async with self.database.sessions() as session:
            member = await session.scalar(
                select(GameParticipantRecord).where(
                    GameParticipantRecord.game_id == game_id,
                    GameParticipantRecord.player_id == player_id,
                )
            )
            if member is None:
                observer = await session.scalar(
                    select(GameObserverRecord).where(
                        GameObserverRecord.game_id == game_id,
                        GameObserverRecord.player_id == player_id,
                        GameObserverRecord.active.is_(True),
                    )
                )
                if observer is None:
                    raise PermissionError("Player is not a game participant or observer")
                return "observer"
            game = await session.get(GameRecord, game_id)
            assert game is not None
            if game.status == "active" and not member.active and member.abandoned_at is not None:
                later_game = await session.scalar(
                    select(GameParticipantRecord.id)
                    .where(
                        GameParticipantRecord.player_id == player_id,
                        GameParticipantRecord.rating_sequence > member.rating_sequence,
                    )
                    .limit(1)
                )
                if later_game is not None:
                    raise PermissionError(
                        "Access to the abandoned game was forfeited by entering another game"
                    )
            return "player"

    @staticmethod
    def _session(connection: ClientConnection) -> PlayerSession:
        if connection.session is None:
            raise PermissionError("Log in first")
        return connection.session

    @staticmethod
    def _participant(session: PlayerSession) -> ParticipantInput:
        return ParticipantInput(session.telegram_user_id, session.public_nickname)

    @staticmethod
    def _require_admin(session: PlayerSession) -> None:
        if not session.admin:
            raise PermissionError("Administrator access is required")

    @staticmethod
    def _string(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def _uuid(params: dict[str, Any], key: str) -> UUID:
        try:
            return UUID(str(params[key]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{key} must be a UUID") from error

    @staticmethod
    def _integer(
        params: dict[str, Any],
        key: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        parsed = value
        if minimum is not None and parsed < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"{key} must be at most {maximum}")
        return parsed

    @staticmethod
    def _optional_datetime(params: dict[str, Any], key: str) -> datetime | None:
        value = params.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be an ISO 8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{key} must be an ISO 8601 timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{key} must include a timezone")
        return parsed

    @staticmethod
    def _boolean(params: dict[str, Any], key: str, *, default: bool) -> bool:
        value = params.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value


async def main(args: argparse.Namespace) -> None:
    mini_app_origins: set[str] | None = None
    website_origins: set[str] = set()
    if args.mini_app_port is not None:
        if not args.bot_token or not args.application_security_key:
            raise RuntimeError(
                "BOT_TOKEN and APPLICATION_SECURITY_KEY are required for the Mini App server"
            )
        mini_app_origins = {
            MiniAppAuthService.normalize_origin(origin)
            for origin in _mini_app_origins(args.mini_app_allowed_origins)
        }
        website_origins = {
            MiniAppAuthService.normalize_origin(origin)
            for origin in _mini_app_origins(getattr(args, "website_allowed_origins", "[]"))
        }
        if {urlsplit(origin).hostname for origin in mini_app_origins} & {
            urlsplit(origin).hostname for origin in website_origins
        }:
            raise ValueError("Website and Mini App domains must be different")
    database = Database(args.database_url)
    launch_references = (
        LaunchReferenceService(
            database,
            signing_key=args.application_security_key,
            bot_id=int(args.bot_token.split(":", 1)[0]),
            environment=args.telegram_environment,
        )
        if args.mini_app_port is not None
        else None
    )
    server = ConsoleApplicationServer(
        database,
        host=args.host,
        port=args.port,
        admin_token=args.admin_token,
        application_client_token=args.application_client_token,
        token_delivery_key=args.token_delivery_key,
        poll_interval=args.poll_interval,
        launch_references=launch_references,
    )
    mini_app: MiniAppHttpServer | None = None
    if args.mini_app_port is not None:
        assert mini_app_origins is not None
        mini_app = MiniAppHttpServer(
            MiniAppAuthService(
                database,
                bot_token=args.bot_token,
                session_signing_key=args.application_security_key,
                allowed_origins=mini_app_origins | website_origins,
                environment=args.telegram_environment,
            ),
            server.application_gateway,
            launch_references=launch_references,
            selection_notifier=TelegramTournamentSelectionNotifier(args.bot_token),
            host=args.mini_app_host,
            port=args.mini_app_port,
            web_dist=Path(args.mini_app_web_dist),
            website_origins=website_origins,
        )
    try:
        if mini_app is not None:
            await mini_app.start()
            LOGGER.info(
                "Mini App server listening on %s:%s",
                args.mini_app_host,
                args.mini_app_port,
            )
        await server.serve_forever()
    finally:
        if mini_app is not None:
            await mini_app.close()
        await server.close()
        await database.close()


def _mini_app_origins(raw: str) -> set[str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [value.strip() for value in raw.split(",") if value.strip()]
    if not isinstance(parsed, list) or not all(isinstance(value, str) for value in parsed):
        raise ValueError("MINI_APP_ALLOWED_ORIGINS must be a JSON array or comma-separated list")
    return set(parsed)


def run() -> None:
    parser = argparse.ArgumentParser(description="Run the SITG localhost application server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://sitg:sitg@localhost:5432/sitg"
        ),
    )
    parser.add_argument("--admin-token", default=os.environ.get("SITG_ADMIN_TOKEN"))
    parser.add_argument(
        "--application-client-token",
        default=os.environ.get("SITG_APPLICATION_CLIENT_TOKEN"),
    )
    parser.add_argument("--token-delivery-key", default=os.environ.get("SITG_TOKEN_DELIVERY_KEY"))
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--mini-app-host", default=os.environ.get("MINI_APP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--mini-app-port",
        type=int,
        default=(int(os.environ["MINI_APP_PORT"]) if os.environ.get("MINI_APP_PORT") else None),
    )
    parser.add_argument("--bot-token", default=os.environ.get("BOT_TOKEN"))
    parser.add_argument(
        "--application-security-key",
        default=os.environ.get("APPLICATION_SECURITY_KEY"),
    )
    parser.add_argument(
        "--mini-app-allowed-origins",
        default=os.environ.get("MINI_APP_ALLOWED_ORIGINS", "[]"),
    )
    parser.add_argument(
        "--mini-app-web-dist",
        default=os.environ.get("MINI_APP_WEB_DIST", "web/dist"),
    )
    parser.add_argument(
        "--website-allowed-origins",
        default=os.environ.get("WEBSITE_ALLOWED_ORIGINS", "[]"),
    )
    parser.add_argument(
        "--telegram-environment",
        choices=("production", "test"),
        default=os.environ.get("TELEGRAM_ENVIRONMENT", "production"),
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    with suppress(KeyboardInterrupt):
        asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    run()
