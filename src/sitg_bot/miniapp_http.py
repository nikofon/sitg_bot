from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from aiohttp import web
from pydantic import ValidationError

from sitg_bot.application.adapters import MiniAppGatewayAdapter
from sitg_bot.application.contracts import (
    ActionCode,
    AdminSuspicionClearOperation,
    AdminSuspicionInspectOperation,
    AdminSuspicionLedgerOperation,
    AuthorsSearchOperation,
    GatewayOperation,
    GatewayResponse,
    LibraryAccessOperation,
    LibraryListOperation,
    LobbyEventsOperation,
    LobbyInfoOperation,
    LobbyPacketOperation,
    LobbyReadyUpdateOperation,
    LobbyRoleUpdateOperation,
    LobbySettingsUpdateOperation,
    LobbySimpleMutationOperation,
    NavigationTournamentSetOperation,
    PacketDraftAuthorCreateOperation,
    PacketDraftDecisionOperation,
    PacketDraftGetOperation,
    PacketDraftUpdateOperation,
    PacketManagementActionOperation,
    PacketManagementGetOperation,
    PacketManagementUpdateOperation,
    TournamentAuthorCreateOperation,
    TournamentCompleteOperation,
    TournamentFinalizeOperation,
    TournamentInfoOperation,
    TournamentListOperation,
    TournamentManagerManagementOperation,
    TournamentManagerSettingsOperation,
    TournamentManagerSettingsUpdateOperation,
    TournamentPacketAccessUpdateOperation,
    TournamentRegisterOperation,
    TournamentRegistrationDecideOperation,
    TournamentRegistrationOverrideOperation,
)
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.launch_references import LaunchReferenceService
from sitg_bot.services.miniapp_auth import (
    MiniAppAuthenticationError,
    MiniAppAuthService,
    MiniAppCsrfError,
    MiniAppSessionContext,
    MiniAppSessionCredentials,
)

SESSION_COOKIE = "__Host-sitg_session"
LOGGER = logging.getLogger(__name__)


class TournamentSelectionNotifier(Protocol):
    async def notify_selected(
        self, telegram_user_id: int, navigation_payload: Mapping[str, object]
    ) -> None: ...

    async def lobby_invitation_url(self, invitation_code: str) -> str: ...

    async def close(self) -> None: ...


class MiniAppHttpServer:
    """Same-origin HTTP boundary for the authenticated tournament Mini App."""

    def __init__(
        self,
        auth: MiniAppAuthService,
        gateway: ApplicationGateway,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        client_version: str = "0.1.0",
        web_dist: Path | None = None,
        launch_references: LaunchReferenceService | None = None,
        selection_notifier: TournamentSelectionNotifier | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("Mini App HTTP port is invalid")
        self.auth = auth
        self.gateway = MiniAppGatewayAdapter(gateway, client_version=client_version)
        self.host = host
        self.port = port
        self.web_dist = web_dist.resolve() if web_dist is not None else None
        self.launch_references = launch_references
        self.selection_notifier = selection_notifier
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def application(self) -> web.Application:
        app = web.Application(client_max_size=5 * 1024 * 1024, middlewares=[self._headers])
        app.router.add_route("OPTIONS", "/api/{path:.*}", self._options)
        app.router.add_post("/api/miniapp/session", self._create_session)
        app.router.add_post("/api/miniapp/session/refresh", self._refresh_session)
        app.router.add_get("/api/miniapp/routes/resolve", self._resolve_route)
        app.router.add_post("/api/miniapp/library/{version_id}/{command}", self._library_access)
        app.router.add_get("/api/miniapp/tournaments/{tournament_id}", self._tournament_info)
        app.router.add_post(
            "/api/miniapp/tournaments/{tournament_id}/register",
            self._register,
        )
        app.router.add_get(
            "/api/miniapp/lobbies/{launch_ref}/events",
            self._lobby_events,
        )
        app.router.add_post(
            "/api/miniapp/tournaments/{tournament_id}/select",
            self._select,
        )
        app.router.add_post(
            "/api/miniapp/lobbies/{launch_ref}/{command}",
            self._mutate_lobby,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/settings",
            self._update_manager_settings,
        )
        app.router.add_get(
            "/api/miniapp/manager/tournaments/{launch_ref}/authors",
            self._search_manager_authors,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/authors",
            self._create_manager_author,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/finalize",
            self._finalize_tournament,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/registration-availability",
            self._set_registration_availability,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/registrations/{player_id}",
            self._decide_registration,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/packet-access",
            self._set_packet_access,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/complete",
            self._complete_tournament,
        )
        app.router.add_get("/api/miniapp/manager/packets/{launch_ref}", self._packet_draft)
        app.router.add_get(
            "/api/miniapp/manager/tournaments/{launch_ref}/packets/{assignment_id}",
            self._management_packet,
        )
        app.router.add_post(
            "/api/miniapp/manager/tournaments/{launch_ref}/packets/{assignment_id}/{command}",
            self._management_packet,
        )
        app.router.add_post("/api/miniapp/manager/packets/{launch_ref}", self._update_packet_draft)
        app.router.add_get(
            "/api/miniapp/manager/packets/{launch_ref}/authors", self._search_packet_authors
        )
        app.router.add_post(
            "/api/miniapp/manager/packets/{launch_ref}/authors", self._create_packet_author
        )
        app.router.add_post(
            "/api/miniapp/manager/packets/{launch_ref}/{decision}",
            self._decide_packet_draft,
        )
        app.router.add_get(
            "/api/miniapp/admin/suspicion/ledger", self._admin_suspicion_ledger
        )
        app.router.add_get(
            "/api/miniapp/admin/suspicion/ledger/{player_id}/events",
            self._admin_suspicion_events,
        )
        app.router.add_post(
            "/api/miniapp/admin/suspicion/ledger/{player_id}/clear",
            self._admin_suspicion_clear,
        )
        if self.web_dist is not None:
            app.router.add_get("/{path:.*}", self._static)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._runner = web.AppRunner(self.application, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        if self.selection_notifier is not None:
            await self.selection_notifier.close()

    @web.middleware
    async def _headers(
        self, request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except (MiniAppAuthenticationError, MiniAppCsrfError) as error:
            response = self._error_response(
                "authentication_required"
                if isinstance(error, MiniAppAuthenticationError)
                else "forbidden",
                401 if isinstance(error, MiniAppAuthenticationError) else 403,
            )
        except (ValidationError, ValueError, TypeError):
            response = self._error_response("validation_failed", 400)
        except PermissionError:
            response = self._error_response("forbidden", 403)
        except LookupError:
            response = self._error_response("not_found", 404)
        except web.HTTPException:
            raise
        except Exception:
            LOGGER.exception("Unhandled Mini App HTTP request path=%s", request.path)
            response = self._error_response("internal_error", 500)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            try:
                response.headers.update(
                    self.auth.security_policy.response_headers(self._origin(request))
                )
            except MiniAppAuthenticationError:
                if response.status < 400:
                    return self._error_response("authentication_required", 401)
        else:
            response.headers["Content-Security-Policy"] = (
                self.auth.security_policy.content_security_policy
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def _options(self, request: web.Request) -> web.Response:
        self.auth.security_policy.response_headers(self._origin(request))
        return web.Response(status=204)

    async def _create_session(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        init_data = body.get("init_data")
        if not isinstance(init_data, str):
            raise ValueError("init_data is required")
        credentials = await self.auth.create_session(init_data, origin=self._origin(request))
        response = web.json_response(self._session_payload(credentials))
        self._set_session_cookie(response, credentials)
        return response

    async def _refresh_session(self, request: web.Request) -> web.Response:
        credentials = await self.auth.refresh_session(
            self._session_token(request),
            origin=self._origin(request),
            csrf_token=request.headers.get("X-CSRF-Token"),
            idempotency_key=request.headers.get("X-Idempotency-Key"),
            content_type=request.content_type,
        )
        response = web.json_response(self._session_payload(credentials))
        self._set_session_cookie(response, credentials)
        return response

    async def _resolve_route(self, request: web.Request) -> web.Response:
        requested_path = request.query.get("path", "")
        parsed = urlsplit(requested_path)
        if parsed.scheme or parsed.netloc:
            return self._route_not_found()
        normalized_path = parsed.path.rstrip("/")
        if normalized_path == "/library" or re.fullmatch(
            r"/library/[0-9a-fA-F-]{36}", normalized_path
        ):
            session, result = await self._query(
                request, LibraryListOperation(action=ActionCode.LIBRARY_LIST)
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            return web.json_response({
                "locale": session.preferred_locale, "authorization": {"allowed": True},
                "resource": {"kind": "library", "state": "ready", **result.data},
            })
        manager_match = re.fullmatch(
            r"/manager/tournaments/([A-Za-z0-9_-]+)/settings", normalized_path
        )
        if normalized_path == "/admin/suspicion":
            session, result = await self._query(
                request, AdminSuspicionLedgerOperation(action=ActionCode.ADMIN_SUSPICION_LEDGER)
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            items = result.data.get("items", [])
            return web.json_response(
                {
                    "locale": session.preferred_locale,
                    "authorization": {"allowed": True},
                    "resource": {
                        "kind": "admin_suspicion_ledger",
                        "state": "ready" if items else "empty",
                        **result.data,
                    },
                }
            )
        management_match = re.fullmatch(
            r"/manager/tournaments/([A-Za-z0-9_-]+)/management", normalized_path
        )
        packet_match = re.fullmatch(r"/manager/packets/([A-Za-z0-9_-]+)/edit", normalized_path)
        lobby_match = re.fullmatch(r"/lobbies/([A-Za-z0-9_-]+)", normalized_path)
        if packet_match is not None:
            session, draft_id = await self._resolve_packet_reference(
                request, packet_match.group(1), action=ActionCode.PACKET_DRAFT_GET
            )
            result = await self.gateway.execute(
                session,
                PacketDraftGetOperation(action=ActionCode.PACKET_DRAFT_GET, draft_id=draft_id),
                correlation_id=self._correlation_id(request),
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            return web.json_response(
                {
                    "locale": session.preferred_locale,
                    "authorization": {"allowed": True},
                    "resource": {
                        "kind": "packet_draft",
                        "state": "ready",
                        **result.data,
                    },
                }
            )
        if lobby_match is not None:
            session, lobby_id = await self._resolve_lobby_reference(
                request, lobby_match.group(1), action=ActionCode.LOBBY_INFO
            )
            result = await self.gateway.execute(
                session,
                LobbyInfoOperation(action=ActionCode.LOBBY_INFO, lobby_id=lobby_id),
                correlation_id=self._correlation_id(request),
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            invitation_url = None
            if self.selection_notifier is not None:
                try:
                    invitation_url = await self.selection_notifier.lobby_invitation_url(
                        str(result.data["invitation_code"])
                    )
                except Exception:
                    LOGGER.exception("Failed to resolve the lobby invitation bot identity")
            return web.json_response(
                {
                    "locale": session.preferred_locale,
                    "authorization": {"allowed": True},
                    "resource": {
                        "kind": "lobby",
                        "invitation_url": invitation_url,
                        "state": "ready" if result.data.get("status") == "assembling" else "empty",
                        **result.data,
                    },
                }
            )
        if manager_match is not None:
            session, tournament_id = await self._resolve_manager_reference(
                request,
                manager_match.group(1),
                action=ActionCode.TOURNAMENT_MANAGER_SETTINGS,
            )
            operation = TournamentManagerSettingsOperation(
                action=ActionCode.TOURNAMENT_MANAGER_SETTINGS,
                tournament_id=tournament_id,
            )
            result = await self.gateway.execute(
                session, operation, correlation_id=self._correlation_id(request)
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            return web.json_response(
                {
                    "locale": session.preferred_locale,
                    "authorization": {"allowed": True},
                    "resource": {
                        "kind": "manager_settings",
                        "state": "ready",
                        **result.data,
                    },
                }
            )
        if management_match is not None:
            session, tournament_id = await self._resolve_manager_reference(
                request,
                management_match.group(1),
                action=ActionCode.TOURNAMENT_MANAGER_MANAGEMENT,
                expected_routes={"manager_management"},
            )
            result = await self.gateway.execute(
                session,
                TournamentManagerManagementOperation(
                    action=ActionCode.TOURNAMENT_MANAGER_MANAGEMENT,
                    tournament_id=tournament_id,
                ),
                correlation_id=self._correlation_id(request),
            )
            if not result.ok:
                return self._gateway_response(result)
            assert isinstance(result.data, dict)
            return web.json_response(
                {
                    "locale": session.preferred_locale,
                    "authorization": {"allowed": True},
                    "resource": {
                        "kind": "manager_management",
                        "state": "ready",
                        **result.data,
                    },
                }
            )
        if normalized_path != "/tournaments":
            return self._route_not_found()
        raw_query = parse_qs(parsed.query, keep_blank_values=False)
        query = {key: values[-1] for key, values in raw_query.items() if values}
        operation = TournamentListOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_LIST,
                "role": query.get("role", "player"),
                "phase": query.get("phase"),
                "relationship": query.get("relationship"),
                "registration": query.get("registration"),
                "type_key": query.get("type"),
                "ruleset_key": query.get("ruleset"),
                "language": query.get("language"),
                "search": query.get("search", ""),
                "order": query.get("order", "starts_asc"),
                "cursor": query.get("cursor"),
                "limit": query.get("limit", 20),
            }
        )
        session, result = await self._query(request, operation)
        if not result.ok:
            return self._gateway_response(result)
        assert isinstance(result.data, dict)
        items = result.data.get("items", [])
        next_cursor = result.data.get("next_cursor")
        return web.json_response(
            {
                "locale": session.preferred_locale,
                "authorization": {"allowed": True},
                "resource": {
                    "kind": "tournaments",
                    "state": "ready" if items else "empty",
                    "role": operation.role,
                    "navigation_version": result.data.get("navigation_version", 0),
                    "total": result.data.get("total", len(items)),
                    "items": items,
                },
                "pagination": {"next": next_cursor} if next_cursor else {},
            }
        )

    async def _tournament_info(self, request: web.Request) -> web.Response:
        operation = TournamentInfoOperation(
            action=ActionCode.TOURNAMENT_INFO,
            tournament_id=self._tournament_id(request),
            role=request.query.get("role", "player"),  # type: ignore[arg-type]
        )
        _, result = await self._query(request, operation)
        return self._gateway_response(result)

    async def _register(self, request: web.Request) -> web.Response:
        operation = TournamentRegisterOperation(
            action=ActionCode.TOURNAMENT_REGISTER,
            tournament_id=self._tournament_id(request),
        )
        _, result = await self._mutation(request, operation)
        return self._gateway_response(result)

    async def _select(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        operation = NavigationTournamentSetOperation.model_validate(
            {
                "action": ActionCode.NAVIGATION_TOURNAMENT_SET,
                "mode": body.get("mode"),
                "tournament_id": self._tournament_id(request),
                "expected_version": body.get("expected_version"),
            }
        )
        session, result = await self._mutation(request, operation)
        if self.selection_notifier is not None and result.ok and result.data is not None:
            try:
                await self.selection_notifier.notify_selected(
                    session.principal.telegram_user_id,
                    result.data,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to push selected tournament context player_id=%s",
                    session.principal.player_id,
                )
        return self._gateway_response(result)

    async def _mutate_lobby(self, request: web.Request) -> web.Response:
        commands = {
            "ready": ActionCode.LOBBY_READY_UPDATE,
            "role": ActionCode.LOBBY_ROLE_UPDATE,
            "settings": ActionCode.LOBBY_SETTINGS_UPDATE,
            "packet-select": ActionCode.LOBBY_PACKET_SELECT,
            "packet-remove": ActionCode.LOBBY_PACKET_REMOVE,
            "leave": ActionCode.LOBBY_LEAVE,
            "cancel": ActionCode.LOBBY_CANCEL,
            "start": ActionCode.LOBBY_START,
            "search-start": ActionCode.LOBBY_SEARCH_START,
            "search-cancel": ActionCode.LOBBY_SEARCH_CANCEL,
        }
        command = request.match_info["command"]
        action = commands.get(command)
        if action is None:
            raise web.HTTPNotFound()
        body = await self._json_body(request)
        session, lobby_id = await self._resolve_lobby_reference(
            request,
            request.match_info["launch_ref"],
            action=action,
            mutation=True,
        )
        common = {
            "action": action,
            "lobby_id": lobby_id,
            "expected_version": body.get("expected_version"),
        }
        if action == ActionCode.LOBBY_READY_UPDATE:
            operation: GatewayOperation = LobbyReadyUpdateOperation.model_validate(
                {**common, "ready": body.get("ready")}
            )
        elif action == ActionCode.LOBBY_ROLE_UPDATE:
            operation = LobbyRoleUpdateOperation.model_validate(
                {
                    **common,
                    "role": body.get("role"),
                    "confirm_fresh": body.get("confirm_fresh", False),
                }
            )
        elif action == ActionCode.LOBBY_SETTINGS_UPDATE:
            operation = LobbySettingsUpdateOperation.model_validate(
                {**common, "changes": body.get("changes")}
            )
        elif action in {ActionCode.LOBBY_PACKET_SELECT, ActionCode.LOBBY_PACKET_REMOVE}:
            operation = LobbyPacketOperation.model_validate(
                {**common, "packet_id": body.get("packet_id")}
            )
        else:
            operation = LobbySimpleMutationOperation.model_validate(common)
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _lobby_events(self, request: web.Request) -> web.Response:
        session, lobby_id = await self._resolve_lobby_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.LOBBY_EVENTS,
        )
        operation = LobbyEventsOperation.model_validate(
            {
                "action": ActionCode.LOBBY_EVENTS,
                "lobby_id": lobby_id,
                "after_sequence": request.query.get("after", "0"),
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _update_manager_settings(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE,
            mutation=True,
        )
        operation = TournamentManagerSettingsUpdateOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE,
                "tournament_id": tournament_id,
                **body,
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _search_manager_authors(self, request: web.Request) -> web.Response:
        session, _ = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.AUTHORS_SEARCH,
            expected_routes={"manager_settings", "manager_management"},
        )
        operation = AuthorsSearchOperation(
            action=ActionCode.AUTHORS_SEARCH,
            query=request.query.get("query", ""),
            limit=20,
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _create_manager_author(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_AUTHOR_CREATE,
            mutation=True,
            expected_routes={"manager_settings", "manager_management"},
        )
        operation = TournamentAuthorCreateOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_AUTHOR_CREATE,
                "tournament_id": tournament_id,
                **body,
                "return_author": request.query.get("return_author") == "true",
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _finalize_tournament(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_FINALIZE,
            mutation=True,
            expected_routes={"manager_settings", "manager_management"},
        )
        operation = TournamentFinalizeOperation.model_validate(
            {"action": ActionCode.TOURNAMENT_FINALIZE, "tournament_id": tournament_id, **body}
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _set_registration_availability(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_REGISTRATION_OVERRIDE,
            mutation=True,
            expected_routes={"manager_management"},
        )
        operation = TournamentRegistrationOverrideOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_REGISTRATION_OVERRIDE,
                "tournament_id": tournament_id,
                **body,
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _decide_registration(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_REGISTRATION_DECIDE,
            mutation=True,
            expected_routes={"manager_management"},
        )
        operation = TournamentRegistrationDecideOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_REGISTRATION_DECIDE,
                "tournament_id": tournament_id,
                "player_id": request.match_info["player_id"],
                "decision": body.get("decision"),
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _set_packet_access(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE,
            mutation=True,
            expected_routes={"manager_management"},
        )
        operation = TournamentPacketAccessUpdateOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE,
                "tournament_id": tournament_id,
                **body,
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _complete_tournament(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, tournament_id = await self._resolve_manager_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.TOURNAMENT_COMPLETE,
            mutation=True,
            expected_routes={"manager_management"},
        )
        operation = TournamentCompleteOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_COMPLETE,
                "tournament_id": tournament_id,
                **body,
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _management_packet(self, request: web.Request) -> web.Response:
        command = request.match_info.get("command", "get")
        actions = {
            "get": ActionCode.PACKET_MANAGEMENT_GET,
            "save": ActionCode.PACKET_MANAGEMENT_UPDATE,
            "delete": ActionCode.PACKET_MANAGEMENT_DELETE,
            "release": ActionCode.PACKET_MANAGEMENT_RELEASE,
        }
        if command not in actions or (request.method == "POST" and command == "get"):
            raise LookupError("Packet command not found")
        mutation = request.method == "POST"
        body = await self._json_body(request) if mutation else {}
        session, tournament_id = await self._resolve_manager_reference(
            request, request.match_info["launch_ref"], action=actions[command],
            mutation=mutation, expected_routes={"manager_management"},
        )
        model = (
            PacketManagementGetOperation if not mutation
            else PacketManagementUpdateOperation if command == "save"
            else PacketManagementActionOperation
        )
        operation = model.model_validate({
            **body, "action": actions[command], "tournament_id": tournament_id,
            "assignment_id": request.match_info["assignment_id"],
        })
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _packet_draft(self, request: web.Request) -> web.Response:
        session, draft_id = await self._resolve_packet_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.PACKET_DRAFT_GET,
        )
        result = await self.gateway.execute(
            session,
            PacketDraftGetOperation(action=ActionCode.PACKET_DRAFT_GET, draft_id=draft_id),
            correlation_id=self._correlation_id(request),
        )
        return self._gateway_response(result)

    async def _update_packet_draft(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, draft_id = await self._resolve_packet_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.PACKET_DRAFT_UPDATE,
            mutation=True,
        )
        operation = PacketDraftUpdateOperation.model_validate(
            {
                **body,
                "action": ActionCode.PACKET_DRAFT_UPDATE,
                "draft_id": draft_id,
            }
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return self._gateway_response(result)

    async def _decide_packet_draft(self, request: web.Request) -> web.Response:
        decision = request.match_info["decision"]
        actions = {
            "publish": ActionCode.PACKET_DRAFT_PUBLISH,
            "reject": ActionCode.PACKET_DRAFT_REJECT,
        }
        action = actions.get(decision)
        if action is None:
            raise LookupError("Packet draft decision not found")
        session, draft_id = await self._resolve_packet_reference(
            request,
            request.match_info["launch_ref"],
            action=action,
            mutation=True,
        )
        result = await self.gateway.execute(
            session,
            PacketDraftDecisionOperation(action=action, draft_id=draft_id),
            correlation_id=self._correlation_id(request),
        )
        return self._gateway_response(result)

    async def _search_packet_authors(self, request: web.Request) -> web.Response:
        session, _ = await self._resolve_packet_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.AUTHORS_SEARCH,
        )
        result = await self.gateway.execute(
            session,
            AuthorsSearchOperation(
                action=ActionCode.AUTHORS_SEARCH,
                query=request.query.get("query", ""),
                limit=20,
            ),
            correlation_id=self._correlation_id(request),
        )
        return self._gateway_response(result)

    async def _create_packet_author(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session, draft_id = await self._resolve_packet_reference(
            request,
            request.match_info["launch_ref"],
            action=ActionCode.PACKET_DRAFT_AUTHOR_CREATE,
            mutation=True,
        )
        result = await self.gateway.execute(
            session,
            PacketDraftAuthorCreateOperation.model_validate(
                {
                    **body,
                    "action": ActionCode.PACKET_DRAFT_AUTHOR_CREATE,
                    "draft_id": draft_id,
                }
            ),
            correlation_id=self._correlation_id(request),
        )
        return self._gateway_response(result)

    async def _resolve_manager_reference(
        self,
        request: web.Request,
        raw_reference: str,
        *,
        action: ActionCode,
        mutation: bool = False,
        expected_routes: set[str] | None = None,
    ) -> tuple[MiniAppSessionContext, UUID]:
        if self.launch_references is None:
            raise LookupError("Launch-reference service is unavailable")
        authorization = {
            "origin": self._origin(request),
            "method": request.method,
            "action": action,
        }
        if mutation:
            authorization.update(
                {
                    "csrf_token": request.headers.get("X-CSRF-Token"),
                    "idempotency_key": request.headers.get("X-Idempotency-Key"),
                    "content_type": request.content_type,
                }
            )
        session = await self.auth.authorize_request(self._session_token(request), **authorization)
        player_id = session.principal.player_id
        if player_id is None:
            raise MiniAppAuthenticationError("Player identity is missing")
        resolved = await self.launch_references.resolve(raw_reference, player_id=player_id)
        routes = expected_routes or {"manager_settings"}
        if resolved.route not in routes:
            raise LookupError("Manager tournament launch reference not found")
        return session, resolved.target_id

    async def _resolve_packet_reference(
        self,
        request: web.Request,
        raw_reference: str,
        *,
        action: ActionCode,
        mutation: bool = False,
    ) -> tuple[MiniAppSessionContext, UUID]:
        if self.launch_references is None:
            raise LookupError("Launch-reference service is unavailable")
        authorization: dict[str, object] = {
            "origin": self._origin(request),
            "method": request.method,
            "action": action,
        }
        if mutation:
            authorization.update(
                {
                    "csrf_token": request.headers.get("X-CSRF-Token"),
                    "idempotency_key": request.headers.get("X-Idempotency-Key"),
                    "content_type": request.content_type,
                }
            )
        session = await self.auth.authorize_request(
            self._session_token(request),
            **authorization,  # type: ignore[arg-type]
        )
        player_id = session.principal.player_id
        if player_id is None:
            raise MiniAppAuthenticationError("Player identity is missing")
        resolved = await self.launch_references.resolve(raw_reference, player_id=player_id)
        if resolved.route != "packet_draft":
            raise LookupError("Packet draft launch reference not found")
        return session, resolved.target_id

    async def _resolve_lobby_reference(
        self,
        request: web.Request,
        raw_reference: str,
        *,
        action: ActionCode,
        mutation: bool = False,
    ) -> tuple[MiniAppSessionContext, UUID]:
        if self.launch_references is None:
            raise LookupError("Launch-reference service is unavailable")
        authorization = {
            "origin": self._origin(request),
            "method": request.method,
            "action": action,
        }
        if mutation:
            authorization.update(
                {
                    "csrf_token": request.headers.get("X-CSRF-Token"),
                    "idempotency_key": request.headers.get("X-Idempotency-Key"),
                    "content_type": request.content_type,
                }
            )
        session = await self.auth.authorize_request(self._session_token(request), **authorization)
        player_id = session.principal.player_id
        if player_id is None:
            raise MiniAppAuthenticationError("Player identity is missing")
        resolved = await self.launch_references.resolve(raw_reference, player_id=player_id)
        if resolved.route != "lobby":
            raise LookupError("Lobby launch reference not found")
        return session, resolved.target_id

    async def _library_access(self, request: web.Request) -> web.Response:
        actions = {"view": ActionCode.LIBRARY_VIEW, "download": ActionCode.LIBRARY_DOWNLOAD}
        command = request.match_info["command"]
        if command not in actions:
            raise web.HTTPNotFound()
        body = await self._json_body(request)
        operation = LibraryAccessOperation.model_validate({
            **body, "action": actions[command], "version_id": request.match_info["version_id"],
        })
        _, result = await self._mutation(request, operation)
        return self._gateway_response(result)

    async def _admin_suspicion_ledger(self, request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit", "50")
        operation = AdminSuspicionLedgerOperation.model_validate(
            {
                "action": ActionCode.ADMIN_SUSPICION_LEDGER,
                "limit": int(raw_limit),
            }
        )
        _, result = await self._query(request, operation)
        return self._gateway_response(result)

    async def _admin_suspicion_events(self, request: web.Request) -> web.Response:
        operation = AdminSuspicionInspectOperation.model_validate(
            {
                "action": ActionCode.ADMIN_SUSPICION_INSPECT,
                "player_id": request.match_info["player_id"],
            }
        )
        _, result = await self._query(request, operation)
        return self._gateway_response(result)

    async def _admin_suspicion_clear(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        operation = AdminSuspicionClearOperation.model_validate(
            {
                "action": ActionCode.ADMIN_SUSPICION_CLEAR,
                "player_id": request.match_info["player_id"],
                "note": body.get("note"),
            }
        )
        _, result = await self._mutation(request, operation)
        return self._gateway_response(result)

    async def _query(
        self, request: web.Request, operation: GatewayOperation
    ) -> tuple[MiniAppSessionContext, GatewayResponse]:
        session = await self.auth.authorize_request(
            self._session_token(request),
            origin=self._origin(request),
            method=request.method,
            action=ActionCode(operation.action),
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return session, result

    async def _mutation(
        self, request: web.Request, operation: GatewayOperation
    ) -> tuple[MiniAppSessionContext, GatewayResponse]:
        session = await self.auth.authorize_request(
            self._session_token(request),
            origin=self._origin(request),
            method=request.method,
            action=ActionCode(operation.action),
            csrf_token=request.headers.get("X-CSRF-Token"),
            idempotency_key=request.headers.get("X-Idempotency-Key"),
            content_type=request.content_type,
        )
        result = await self.gateway.execute(
            session, operation, correlation_id=self._correlation_id(request)
        )
        return session, result

    async def _static(self, request: web.Request) -> web.StreamResponse:
        assert self.web_dist is not None
        relative = request.match_info.get("path", "")
        candidate = (self.web_dist / relative).resolve()
        if relative and candidate.is_relative_to(self.web_dist) and candidate.is_file():
            return web.FileResponse(candidate)
        index = self.web_dist / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(index)

    @staticmethod
    async def _json_body(request: web.Request) -> dict[str, object]:
        try:
            value = await request.json()
        except Exception as error:
            raise ValueError("Request body must be JSON") from error
        if not isinstance(value, dict):
            raise ValueError("Request body must be an object")
        return value

    def _origin(self, request: web.Request) -> str:
        origin = request.headers.get("Origin")
        if origin:
            return origin
        referer = request.headers.get("Referer")
        if referer:
            parsed = urlsplit(referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        for allowed in self.auth.security_policy.allowed_origins:
            if urlsplit(allowed).netloc.casefold() == request.host.casefold():
                return allowed
        raise MiniAppAuthenticationError("Request origin is missing")

    @staticmethod
    def _session_token(request: web.Request) -> str:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise MiniAppAuthenticationError("Authentication token is missing")
        return token

    @staticmethod
    def _tournament_id(request: web.Request) -> UUID:
        try:
            return UUID(request.match_info["tournament_id"])
        except (KeyError, ValueError) as error:
            raise ValueError("Tournament ID is invalid") from error

    @staticmethod
    def _correlation_id(request: web.Request) -> UUID:
        raw = request.headers.get("X-Correlation-ID")
        if raw is None:
            return uuid4()
        try:
            return UUID(raw)
        except ValueError as error:
            raise ValueError("Correlation ID is invalid") from error

    @staticmethod
    def _session_payload(credentials: MiniAppSessionCredentials) -> dict[str, str]:
        return {
            "csrf_token": credentials.csrf_token,
            "expires_at": credentials.expires_at.isoformat(),
            "locale": credentials.locale,
        }

    @staticmethod
    def _set_session_cookie(response: web.Response, credentials: MiniAppSessionCredentials) -> None:
        response.set_cookie(
            credentials.cookie_name,
            credentials.session_token,
            path=credentials.cookie_path,
            secure=credentials.cookie_secure,
            httponly=credentials.cookie_http_only,
            samesite=credentials.cookie_same_site,
            max_age=max(
                1,
                int((credentials.expires_at - datetime.now(UTC)).total_seconds()),
            ),
        )

    @classmethod
    def _gateway_response(cls, response: GatewayResponse) -> web.Response:
        if response.ok:
            return web.json_response(response.data)
        assert response.error is not None
        statuses = {
            "authentication_required": 401,
            "forbidden": 403,
            "not_found": 404,
            "stale_write": 409,
            "idempotency_conflict": 409,
            "request_in_progress": 409,
        }
        return web.json_response(
            {
                "error": {
                    "code": response.error.code.value,
                    "message_key": response.error.message_key,
                    "retryable": response.error.retryable,
                    "correlation_id": str(response.correlation_id),
                }
            },
            status=statuses.get(response.error.code.value, 400),
        )

    @staticmethod
    def _error_response(code: str, status: int) -> web.Response:
        return web.json_response({"error": {"code": code, "retryable": False}}, status=status)

    @staticmethod
    def _route_not_found() -> web.Response:
        return web.json_response(
            {
                "locale": "ru",
                "authorization": {"allowed": False, "reason_code": "not_found"},
                "resource": {"state": "empty"},
            }
        )
