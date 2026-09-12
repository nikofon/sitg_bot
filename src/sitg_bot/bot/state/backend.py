import base64
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sitg_bot.application.adapters import TelegramGatewayAdapter
from sitg_bot.application.contracts import (
    ActionCode,
    AdminAuthenticateOperation,
    CapabilitiesOperation,
    CapabilityPayload,
    ChatMembersOperation,
    ChatSendOperation,
    GameActOperation,
    GameAppealDecideOperation,
    GameAppealTicketsOperation,
    GameViewOperation,
    GatewayOperation,
    GatewayResponse,
    LobbyCreateOperation,
    LobbyInfoOperation,
    LobbyInviteOperation,
    LobbyJoinOperation,
    LobbyLinkOperation,
    LobbyReadyUpdateOperation,
    LobbySimpleMutationOperation,
    NavigationContextSetOperation,
    NavigationModeSetOperation,
    NavigationSnapshotOperation,
    NavigationTournamentSetOperation,
    NotificationAlertsClaimOperation,
    NotificationReadOperation,
    NotificationsListOperation,
    NotificationsReadAllOperation,
    PacketDraftDecisionOperation,
    PacketDraftTelegramBindOperation,
    PacketUploadEligibilityOperation,
    PacketUploadOperation,
    PlayerResolveOperation,
    RegistrationCompleteOperation,
    RegistrationStartOperation,
    RegistrationStepSaveOperation,
    SettingsListOperation,
    SettingsUpdateOperation,
    TokenClaimOperation,
    TokenInventoryOperation,
    TokenRequestAdminDecideOperation,
    TokenRequestCreateOperation,
    TokenRequestQueueOperation,
    TournamentCreateOperation,
    TournamentInfoOperation,
    TournamentManagerManagementLinkOperation,
    TournamentManagerSettingsLinkOperation,
)
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.state.models import (
    AccountState,
    DeliveredCreationTokenState,
    LobbyCreatedState,
    NavigationState,
    NotificationAlertState,
    NotificationPageState,
    NotificationReadResultState,
    NotificationState,
    PacketDraftState,
    RegistrationState,
    SettingsState,
    TokenInventoryState,
    TokenRequestPageState,
    TokenRequestState,
    TournamentCreatedState,
    TournamentSettingsLinkState,
)


class GatewayCallError(RuntimeError):
    def __init__(self, response: GatewayResponse) -> None:
        if response.error is None:
            raise ValueError("A gateway failure must contain an error")
        super().__init__(response.error.message_key)
        self.error = response.error


class BotBackend:
    """Typed Telegram-facing projection of application gateway operations."""

    def __init__(self, gateway: TelegramGatewayAdapter) -> None:
        self.gateway = gateway

    async def navigation(self, claim: TelegramUpdateClaim) -> NavigationState | None:
        response = await self._execute(
            claim, NavigationSnapshotOperation(action=ActionCode.NAVIGATION_SNAPSHOT)
        )
        if response.data is None:
            return None
        return NavigationState.model_validate(response.data)

    async def authenticate_admin(
        self, claim: TelegramUpdateClaim, *, credential: str
    ) -> NavigationState:
        response = await self._execute(
            claim,
            AdminAuthenticateOperation(
                action=ActionCode.ADMIN_AUTHENTICATE,
                credential=credential,
            ),
        )
        return NavigationState.model_validate(response.data)

    async def admin_authentication_available(self, claim: TelegramUpdateClaim) -> bool:
        response = await self._execute(claim, CapabilitiesOperation(action=ActionCode.CAPABILITIES))
        capabilities = CapabilityPayload.model_validate(response.data)
        return any(item.action == ActionCode.ADMIN_AUTHENTICATE for item in capabilities.actions)

    async def start_registration(
        self, claim: TelegramUpdateClaim, *, telegram_username: str | None
    ) -> RegistrationState:
        response = await self._execute(
            claim,
            RegistrationStartOperation(
                action=ActionCode.REGISTRATION_START,
                telegram_username=telegram_username,
            ),
        )
        return RegistrationState.model_validate(response.data)

    async def save_registration_step(
        self,
        claim: TelegramUpdateClaim,
        *,
        step: str,
        value: str | bool,
        expected_version: int,
    ) -> RegistrationState:
        operation = RegistrationStepSaveOperation.model_validate(
            {
                "action": ActionCode.REGISTRATION_STEP_SAVE,
                "step": step,
                "value": value,
                "expected_version": expected_version,
            }
        )
        response = await self._execute(claim, operation)
        return RegistrationState.model_validate(response.data)

    async def complete_registration(
        self, claim: TelegramUpdateClaim, *, expected_version: int
    ) -> AccountState:
        response = await self._execute(
            claim,
            RegistrationCompleteOperation(
                action=ActionCode.REGISTRATION_COMPLETE,
                expected_version=expected_version,
            ),
        )
        return AccountState.model_validate(response.data)

    async def update_language(
        self,
        claim: TelegramUpdateClaim,
        *,
        locale: str,
        expected_version: int,
    ) -> RegistrationState | None:
        response = await self._execute(
            claim,
            SettingsUpdateOperation(
                action=ActionCode.SETTINGS_UPDATE,
                key="language",
                value=locale,
                expected_version=expected_version,
            ),
        )
        if isinstance(response.data, dict) and "account" in response.data:
            return RegistrationState.model_validate(response.data)
        return None

    async def set_mode(
        self,
        claim: TelegramUpdateClaim,
        *,
        mode: str,
        expected_version: int,
    ) -> NavigationState:
        operation = NavigationModeSetOperation.model_validate(
            {
                "action": ActionCode.NAVIGATION_MODE_SET,
                "mode": mode,
                "expected_version": expected_version,
            }
        )
        response = await self._execute(claim, operation)
        return NavigationState.model_validate(response.data)

    async def select_tournament(
        self,
        claim: TelegramUpdateClaim,
        *,
        mode: str,
        tournament_id: UUID | None,
        expected_version: int,
    ) -> NavigationState:
        operation = NavigationTournamentSetOperation.model_validate(
            {
                "action": ActionCode.NAVIGATION_TOURNAMENT_SET,
                "mode": mode,
                "tournament_id": tournament_id,
                "expected_version": expected_version,
            }
        )
        response = await self._execute(claim, operation)
        return NavigationState.model_validate(response.data)

    async def settings(self, claim: TelegramUpdateClaim) -> SettingsState:
        response = await self._execute(
            claim, SettingsListOperation(action=ActionCode.SETTINGS_LIST)
        )
        return SettingsState.model_validate(response.data)

    async def notifications(
        self,
        claim: TelegramUpdateClaim,
        *,
        audience: Literal["player", "manager", "admin"] = "player",
        read_state: Literal["all", "seen", "unseen"] = "all",
        cursor: str | None = None,
        limit: int = 20,
    ) -> NotificationPageState:
        response = await self._execute(
            claim,
            NotificationsListOperation(
                action=ActionCode.NOTIFICATIONS_LIST,
                audience=audience,
                read_state=read_state,
                cursor=cursor,
                limit=limit,
            ),
        )
        return NotificationPageState.model_validate(response.data)

    async def mark_notification_read(
        self, claim: TelegramUpdateClaim, notification_id: UUID
    ) -> NotificationState:
        response = await self._execute(
            claim,
            NotificationReadOperation(
                action=ActionCode.NOTIFICATIONS_READ, notification_id=notification_id
            ),
        )
        return NotificationState.model_validate(response.data)

    async def mark_notifications_read(
        self,
        claim: TelegramUpdateClaim,
        *,
        audience: Literal["player", "manager", "admin"],
    ) -> NotificationReadResultState:
        response = await self._execute(
            claim,
            NotificationsReadAllOperation(
                action=ActionCode.NOTIFICATIONS_READ_ALL,
                audience=audience,
            ),
        )
        return NotificationReadResultState.model_validate(response.data)

    async def claim_notification_alerts(
        self,
        claim: TelegramUpdateClaim,
        *,
        audience: Literal["player", "manager", "admin"],
    ) -> NotificationAlertState:
        response = await self._execute(
            claim,
            NotificationAlertsClaimOperation(
                action=ActionCode.NOTIFICATION_ALERTS_CLAIM, audience=audience
            ),
        )
        return NotificationAlertState.model_validate(response.data)

    async def update_setting(
        self,
        claim: TelegramUpdateClaim,
        *,
        key: str,
        value: str | bool,
        expected_version: int,
    ) -> SettingsState:
        operation = SettingsUpdateOperation.model_validate(
            {
                "action": ActionCode.SETTINGS_UPDATE,
                "key": key,
                "value": value,
                "expected_version": expected_version,
            }
        )
        response = await self._execute(claim, operation)
        return SettingsState.model_validate(response.data)

    async def request_tournament_token(
        self,
        claim: TelegramUpdateClaim,
        *,
        tournament_name: str,
        commentary: str | None = None,
    ) -> TokenRequestState:
        response = await self._execute(
            claim,
            TokenRequestCreateOperation(
                action=ActionCode.TOKEN_REQUEST_CREATE,
                tournament_name=tournament_name,
                justification=commentary,
            ),
        )
        return TokenRequestState.model_validate(response.data)

    async def token_inventory(
        self,
        claim: TelegramUpdateClaim,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> TokenInventoryState:
        response = await self._execute(
            claim,
            TokenInventoryOperation(
                action=ActionCode.TOKEN_INVENTORY,
                cursor=cursor,
                limit=limit,
            ),
        )
        return TokenInventoryState.model_validate(response.data)

    async def claim_tournament_token(
        self, claim: TelegramUpdateClaim, *, request_id: str
    ) -> DeliveredCreationTokenState:
        operation = TokenClaimOperation.model_validate(
            {
                "action": ActionCode.TOKEN_CLAIM,
                "request_id": request_id,
            }
        )
        response = await self._execute(claim, operation)
        return DeliveredCreationTokenState.model_validate(response.data)

    async def pending_token_requests(
        self,
        claim: TelegramUpdateClaim,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> TokenRequestPageState:
        response = await self._execute(
            claim,
            TokenRequestQueueOperation(
                action=ActionCode.TOKEN_REQUEST_ADMIN_PENDING,
                cursor=cursor,
                limit=limit,
            ),
        )
        return TokenRequestPageState.model_validate(response.data)

    async def decide_token_request(
        self,
        claim: TelegramUpdateClaim,
        *,
        request_id: UUID,
        approve: bool,
        commentary: str | None = None,
    ) -> TokenRequestState:
        response = await self._execute(
            claim,
            TokenRequestAdminDecideOperation(
                action=ActionCode.TOKEN_REQUEST_ADMIN_DECIDE,
                request_id=request_id,
                approve=approve,
                note=commentary,
            ),
        )
        return TokenRequestState.model_validate(response.data)

    async def create_tournament(
        self,
        claim: TelegramUpdateClaim,
        *,
        token_id: UUID,
        name: str,
        slug: str,
        type_key: str,
        game_ruleset_key: str,
        visibility: str,
        language: str,
        registration_ends_at: datetime | None = None,
        starts_at: datetime | None = None,
        planned_ends_at: datetime | None = None,
    ) -> TournamentCreatedState:
        operation = TournamentCreateOperation.model_validate(
            {
                "action": ActionCode.TOURNAMENT_CREATE,
                "token_id": token_id,
                "name": name,
                "slug": slug,
                "type_key": type_key,
                "game_ruleset_key": game_ruleset_key,
                "visibility": visibility,
                "language": language,
                "registration_ends_at": registration_ends_at,
                "starts_at": starts_at,
                "planned_ends_at": planned_ends_at,
            }
        )
        response = await self._execute(claim, operation)
        data = cast(dict[str, object], response.data)
        return TournamentCreatedState.model_validate(
            {"id": data["id"], "name": data["name"], "slug": data["slug"]}
        )

    async def tournament_settings_link(
        self, claim: TelegramUpdateClaim
    ) -> TournamentSettingsLinkState:
        response = await self._execute(
            claim,
            TournamentManagerSettingsLinkOperation(
                action=ActionCode.TOURNAMENT_MANAGER_SETTINGS_LINK
            ),
        )
        return TournamentSettingsLinkState.model_validate(response.data)

    async def tournament_management_link(
        self, claim: TelegramUpdateClaim
    ) -> TournamentSettingsLinkState:
        response = await self._execute(
            claim,
            TournamentManagerManagementLinkOperation(
                action=ActionCode.TOURNAMENT_MANAGER_MANAGEMENT_LINK
            ),
        )
        return TournamentSettingsLinkState.model_validate(response.data)

    async def packet_upload_eligibility(
        self, claim: TelegramUpdateClaim, *, tournament_id: UUID
    ) -> dict[str, object]:
        response = await self._execute(
            claim,
            PacketUploadEligibilityOperation(
                action=ActionCode.PACKET_UPLOAD_ELIGIBILITY,
                tournament_id=tournament_id,
            ),
        )
        return cast(dict[str, object], response.data)

    async def upload_packet(
        self,
        claim: TelegramUpdateClaim,
        *,
        tournament_id: UUID,
        source_filename: str,
        source: bytes,
    ) -> PacketDraftState:
        response = await self._execute(
            claim,
            PacketUploadOperation(
                action=ActionCode.PACKET_UPLOAD,
                tournament_id=tournament_id,
                source_filename=source_filename,
                source_base64=base64.b64encode(source).decode("ascii"),
            ),
        )
        return PacketDraftState.model_validate(response.data)

    async def decide_packet_draft(
        self,
        claim: TelegramUpdateClaim,
        *,
        draft_id: UUID,
        publish: bool,
    ) -> dict[str, object]:
        response = await self._execute(
            claim,
            PacketDraftDecisionOperation(
                action=(
                    ActionCode.PACKET_DRAFT_PUBLISH if publish else ActionCode.PACKET_DRAFT_REJECT
                ),
                draft_id=draft_id,
            ),
        )
        return cast(dict[str, object], response.data)

    async def bind_packet_draft_message(
        self,
        claim: TelegramUpdateClaim,
        *,
        draft_id: UUID,
        chat_id: int,
        message_id: int,
        locale: str,
    ) -> None:
        await self._execute(
            claim,
            PacketDraftTelegramBindOperation(
                action=ActionCode.PACKET_DRAFT_TELEGRAM_BIND,
                draft_id=draft_id,
                chat_id=chat_id,
                message_id=message_id,
                locale=locale,
            ),
        )

    async def tournament_info(
        self, claim: TelegramUpdateClaim, *, tournament_id: UUID
    ) -> dict[str, object]:
        response = await self._execute(
            claim,
            TournamentInfoOperation(
                action=ActionCode.TOURNAMENT_INFO,
                tournament_id=tournament_id,
                role="player",
            ),
        )
        return cast(dict[str, object], response.data)

    async def resolve_player(
        self, claim: TelegramUpdateClaim, *, reference: str
    ) -> dict[str, object]:
        response = await self._execute(
            claim,
            PlayerResolveOperation(
                action=ActionCode.PLAYER_RESOLVE,
                reference=reference,
            ),
        )
        return cast(dict[str, object], response.data)

    async def create_lobby(
        self,
        claim: TelegramUpdateClaim,
        *,
        tournament_id: UUID,
        max_players: int = 4,
    ) -> LobbyCreatedState:
        response = await self._execute(
            claim,
            LobbyCreateOperation(
                action=ActionCode.LOBBY_CREATE,
                tournament_id=tournament_id,
                max_players=max_players,
            ),
        )
        data = cast(dict[str, object], response.data)
        lobby = cast(dict[str, object], data["lobby"])
        reference = cast(dict[str, object], data["launch_reference"])
        return LobbyCreatedState.model_validate(
            {
                "lobby_id": lobby["id"],
                "version": lobby["version"],
                "tournament_id": lobby["tournament_id"],
                "status": lobby["status"],
                "launch_reference": reference["value"],
                "launch_expires_at": reference["expires_at"],
            }
        )

    async def lobby_info(self, claim: TelegramUpdateClaim, *, lobby_id: UUID) -> dict:
        response = await self._execute(
            claim, LobbyInfoOperation(action=ActionCode.LOBBY_INFO, lobby_id=lobby_id)
        )
        return cast(dict, response.data)

    async def lobby_context(
        self, claim: TelegramUpdateClaim, *, context: str, expected_version: int
    ) -> NavigationState:
        response = await self._execute(
            claim,
            NavigationContextSetOperation.model_validate(
                {
                    "action": ActionCode.NAVIGATION_CONTEXT_SET,
                    "context": context,
                    "expected_version": expected_version,
                }
            ),
        )
        return NavigationState.model_validate(response.data)

    async def lobby_action(
        self,
        claim: TelegramUpdateClaim,
        *,
        lobby_id: UUID,
        action: str,
        expected_version: int,
        **values: object,
    ) -> dict:
        codes = {
            "ready": ActionCode.LOBBY_READY_UPDATE,
            "unready": ActionCode.LOBBY_READY_UPDATE,
            "start": ActionCode.LOBBY_START,
            "search": ActionCode.LOBBY_SEARCH_START,
            "search_cancel": ActionCode.LOBBY_SEARCH_CANCEL,
            "leave": ActionCode.LOBBY_LEAVE,
            "cancel": ActionCode.LOBBY_CANCEL,
            "invite": ActionCode.LOBBY_INVITE,
        }
        operation_type = LobbySimpleMutationOperation
        if action in {"ready", "unready"}:
            operation_type = LobbyReadyUpdateOperation
            values["ready"] = action == "ready"
        elif action == "invite":
            operation_type = LobbyInviteOperation
        response = await self._execute(
            claim,
            operation_type.model_validate(
                {
                    "action": codes[action],
                    "lobby_id": lobby_id,
                    "expected_version": expected_version,
                    **values,
                }
            ),
        )
        return cast(dict, response.data)

    async def join_lobby(
        self,
        claim: TelegramUpdateClaim,
        *,
        invitation_code: str,
        role: str = "player",
        confirm_fresh: bool = False,
    ) -> None:
        await self._execute(
            claim,
            LobbyJoinOperation.model_validate(
                {
                    "action": ActionCode.LOBBY_JOIN,
                    "invitation_code": invitation_code,
                    "role": role,
                    "confirm_fresh": confirm_fresh,
                }
            ),
        )

    async def lobby_link(
        self, claim: TelegramUpdateClaim, *, lobby_id: UUID
    ) -> TournamentSettingsLinkState:
        response = await self._execute(
            claim,
            LobbyLinkOperation(action=ActionCode.LOBBY_LINK, lobby_id=lobby_id),
        )
        return TournamentSettingsLinkState.model_validate(response.data)

    async def game_appeal_tickets(self, claim: TelegramUpdateClaim) -> list[dict]:
        response = await self._execute(
            claim, GameAppealTicketsOperation(action=ActionCode.GAME_APPEAL_TICKETS)
        )
        return cast(list[dict], response.data)

    async def game_appeal_decide(
        self, claim: TelegramUpdateClaim, appeal_id: UUID, approve: bool
    ) -> dict:
        response = await self._execute(
            claim,
            GameAppealDecideOperation(
                action=ActionCode.GAME_APPEAL_DECIDE, appeal_id=appeal_id, approve=approve
            ),
        )
        return cast(dict, response.data)

    async def chat_members(self, claim, *, scope, scope_id):
        response = await self._execute(
            claim, ChatMembersOperation(
                action=ActionCode.CHAT_MEMBERS, scope=scope, scope_id=scope_id
            )
        )
        return response.data

    async def chat_send(self, claim, **values):
        response = await self._execute(
            claim, ChatSendOperation(action=ActionCode.CHAT_SEND, **values)
        )
        return response.data

    async def game_view(
        self, claim: TelegramUpdateClaim, game_id: UUID | None = None, *, reconnect: bool = False
    ) -> dict:
        response = await self._execute(
            claim,
            GameViewOperation(action=ActionCode.GAME_VIEW, game_id=game_id, reconnect=reconnect),
        )
        return cast(dict, response.data)

    async def game_action(
        self, claim: TelegramUpdateClaim, *, game_id: UUID, command: str, **values: object
    ) -> dict:
        response = await self._execute(
            claim,
            GameActOperation.model_validate(
                {
                    "action": ActionCode.GAME_ACT,
                    "game_id": game_id,
                    "command": command,
                    **values,
                }
            ),
        )
        return cast(dict, response.data)

    async def _execute(
        self, claim: TelegramUpdateClaim, operation: GatewayOperation
    ) -> GatewayResponse:
        response = await self.gateway.execute_update(claim, operation)
        if not response.ok:
            raise GatewayCallError(response)
        return cast(GatewayResponse, response)
