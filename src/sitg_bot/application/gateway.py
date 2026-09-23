import base64
import binascii
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select

from sitg_bot.application.contracts import (
    AccountLookupOperation,
    ActionCode,
    AdminAuthenticateOperation,
    AdminAuthorLinkOperation,
    AdminAuthorMergeOperation,
    AdminManagementListOperation,
    AdminPacketAccessOperation,
    AdminSuspicionClearOperation,
    AdminSuspicionInspectOperation,
    AdminSuspicionLedgerOperation,
    AdminTournamentModerateOperation,
    AdminTournamentRatingWeightOperation,
    ApplicationPrincipal,
    AuthorLinkAdminDecideOperation,
    AuthorLinkAdminPendingOperation,
    AuthorLinkCancelOperation,
    AuthorLinkCreateOperation,
    AuthorLinkMineOperation,
    AuthorsSearchOperation,
    BugReportCreateOperation,
    CapabilityAction,
    CapabilityPayload,
    ChatMembersOperation,
    ChatSendOperation,
    ErrorCode,
    GameActOperation,
    GameAppealDecideOperation,
    GameAppealTicketsOperation,
    GameObserveOperation,
    GameViewOperation,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    LibraryAccessOperation,
    LibraryListOperation,
    LobbyCreateOperation,
    LobbyEventsOperation,
    LobbyInfoOperation,
    LobbyInviteOperation,
    LobbyJoinOperation,
    LobbyLinkOperation,
    LobbyPacketBulkSelectOperation,
    LobbyPacketOperation,
    LobbyReadyUpdateOperation,
    LobbyRoleUpdateOperation,
    LobbySettingsUpdateOperation,
    LobbySimpleMutationOperation,
    NavigationContextSetOperation,
    NavigationModeSetOperation,
    NavigationSnapshotOperation,
    NavigationTournamentSetOperation,
    NotificationAlertsClaimOperation,
    NotificationReadOperation,
    NotificationsListOperation,
    NotificationsReadAllOperation,
    OngoingListOperation,
    PacketDraftAuthorCreateOperation,
    PacketDraftDecisionOperation,
    PacketDraftGetOperation,
    PacketDraftTelegramBindOperation,
    PacketDraftUpdateOperation,
    PacketExistingAddOperation,
    PacketExistingPreviewOperation,
    PacketManagementActionOperation,
    PacketManagementGetOperation,
    PacketManagementUpdateOperation,
    PacketUploadEligibilityOperation,
    PacketUploadOperation,
    PlayerBanOperation,
    PlayerGameResultsOperation,
    PlayerListOperation,
    PlayerProfileOperation,
    PlayerReportOperation,
    PlayerResolveOperation,
    PlayerUnbanOperation,
    RegistrationCompleteOperation,
    RegistrationStartOperation,
    RegistrationStepSaveOperation,
    ReputationVoteOperation,
    SettingsUpdateOperation,
    TelegramUsernameRefreshOperation,
    TokenClaimOperation,
    TokenRequestAdminDecideOperation,
    TokenRequestCreateOperation,
    TokenRequestQueueOperation,
    TournamentAuthorCreateOperation,
    TournamentChatGameTimeOperation,
    TournamentChatInfoOperation,
    TournamentChatListOperation,
    TournamentChatOpenOperation,
    TournamentChatQuitOperation,
    TournamentClassicUpdateOperation,
    TournamentCompleteOperation,
    TournamentCreateOperation,
    TournamentFinalizeOperation,
    TournamentInfoOperation,
    TournamentListOperation,
    TournamentManagerManagementLinkOperation,
    TournamentManagerManagementOperation,
    TournamentManagerSettingsLinkOperation,
    TournamentManagerSettingsOperation,
    TournamentManagerSettingsUpdateOperation,
    TournamentPacketAccessUpdateOperation,
    TournamentProfileOperation,
    TournamentRegisterOperation,
    TournamentRegistrationDecideOperation,
    TournamentRegistrationInvitationOperation,
    TournamentRegistrationLinkOperation,
    TournamentRegistrationOverrideOperation,
    TournamentStartOperation,
)
from sitg_bot.services.admin_auth import PlatformAdminAuthenticationService
from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.launch_references import LaunchReferenceService
from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.matchmaking import InvitationMatchmakingService, LobbyReadinessError
from sitg_bot.services.moderation import BugReportService, PlayerModerationService
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.persistent_game import ParticipantInput
from sitg_bot.services.players import PlayerAccountService, ProfileVersionConflict
from sitg_bot.services.profiles import PlayerProfileService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.token_requests import (
    TokenPlaintextUnavailable,
    TournamentTokenRequestService,
)
from sitg_bot.services.tournament_profiles import TournamentProfileService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.services.trust import TrustService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationIdempotencyRecord,
    ApplicationRequestAuditRecord,
    PlayerRecord,
    PregameLobbyEventRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    mutation: bool = False
    authentication_required: bool = True
    idempotency_required: bool = False
    stale_write_field: str | None = None
    cursor_paginated: bool = False
    sensitive_response: bool = False


class _AuthenticationRequired(PermissionError):
    pass


class _IdempotencyRequired(ValueError):
    pass


class _IdempotencyConflict(ValueError):
    pass


class _RequestInProgress(ValueError):
    pass


class _RequestIndeterminate(ValueError):
    pass


class _CapabilityUnavailable(RuntimeError):
    pass


class _SecretAlreadyDelivered(ValueError):
    pass


ACTION_POLICIES: dict[ActionCode, ActionPolicy] = {action: ActionPolicy() for action in ActionCode}
# Banned players keep read access to their packet library; every other action is refused.
BAN_EXEMPT_ACTIONS = frozenset(
    {
        ActionCode.LIBRARY_LIST,
        ActionCode.LIBRARY_VIEW,
        ActionCode.LIBRARY_DOWNLOAD,
    }
)
ACTION_POLICIES.update(
    {
        ActionCode.CAPABILITIES: ActionPolicy(authentication_required=False),
        ActionCode.GAME_ACT: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.GAME_OBSERVE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.CHAT_SEND: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_CHAT_OPEN: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_CHAT_QUIT: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_CHAT_GAME_TIME: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.GAME_APPEAL_DECIDE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.ADMIN_AUTHENTICATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.REGISTRATION_START: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.REGISTRATION_STEP_SAVE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
            sensitive_response=True,
        ),
        ActionCode.REGISTRATION_COMPLETE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TELEGRAM_USERNAME_REFRESH: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.AUTHORS_SEARCH: ActionPolicy(cursor_paginated=True),
        ActionCode.AUTHOR_LINK_MINE: ActionPolicy(cursor_paginated=True),
        ActionCode.AUTHOR_LINK_ADMIN_PENDING: ActionPolicy(cursor_paginated=True),
        ActionCode.TOKEN_REQUEST_ADMIN_PENDING: ActionPolicy(cursor_paginated=True),
        ActionCode.TOKEN_REQUEST_ADMIN_RESOLVED: ActionPolicy(cursor_paginated=True),
        ActionCode.TOKEN_INVENTORY: ActionPolicy(cursor_paginated=True),
        ActionCode.NOTIFICATIONS_LIST: ActionPolicy(cursor_paginated=True),
        ActionCode.TOURNAMENT_LIST: ActionPolicy(
            authentication_required=False, cursor_paginated=True
        ),
        ActionCode.TOURNAMENT_INFO: ActionPolicy(authentication_required=False),
        ActionCode.TOURNAMENT_PROFILE: ActionPolicy(authentication_required=False),
        ActionCode.PLAYER_LIST: ActionPolicy(authentication_required=False),
        ActionCode.PLAYER_PROFILE: ActionPolicy(authentication_required=False),
        ActionCode.PLAYER_GAME_RESULTS: ActionPolicy(authentication_required=False),
        ActionCode.SETTINGS_UPDATE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
            sensitive_response=True,
        ),
        ActionCode.NAVIGATION_MODE_SET: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.NAVIGATION_TOURNAMENT_SET: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.AUTHOR_LINK_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.AUTHOR_LINK_CANCEL: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.AUTHOR_LINK_ADMIN_DECIDE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_status"
        ),
        ActionCode.TOKEN_REQUEST_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOKEN_REQUEST_ADMIN_DECIDE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_status"
        ),
        ActionCode.TOKEN_CLAIM: ActionPolicy(
            mutation=True, idempotency_required=True, sensitive_response=True
        ),
        ActionCode.TOURNAMENT_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_REGISTER: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TOURNAMENT_AUTHOR_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.TOURNAMENT_MANAGER_SETTINGS_LINK: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.TOURNAMENT_FINALIZE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TOURNAMENT_MANAGER_MANAGEMENT_LINK: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.TOURNAMENT_REGISTRATION_OVERRIDE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TOURNAMENT_CLASSIC_UPDATE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TOURNAMENT_REGISTRATION_DECIDE: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.TOURNAMENT_COMPLETE: ActionPolicy(
            mutation=True,
            idempotency_required=True,
            stale_write_field="expected_version",
        ),
        ActionCode.TOURNAMENT_START: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version",
        ),
        ActionCode.PACKET_UPLOAD: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.LIBRARY_VIEW: ActionPolicy(
            mutation=True, idempotency_required=True, sensitive_response=True
        ),
        ActionCode.LIBRARY_DOWNLOAD: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.PACKET_MANAGEMENT_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.PACKET_EXISTING_ADD: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version_id"
        ),
        ActionCode.PACKET_MANAGEMENT_DELETE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.PACKET_MANAGEMENT_RELEASE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.PACKET_DRAFT_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.PACKET_DRAFT_TELEGRAM_BIND: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.PACKET_DRAFT_AUTHOR_CREATE: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.PACKET_DRAFT_PUBLISH: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.PACKET_DRAFT_REJECT: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.NOTIFICATIONS_READ: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.NOTIFICATIONS_READ_ALL: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.NOTIFICATION_ALERTS_CLAIM: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.PLAYER_BAN: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.ADMIN_MANAGEMENT_LIST: ActionPolicy(sensitive_response=True),
        ActionCode.ADMIN_TOURNAMENT_MODERATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.ADMIN_TOURNAMENT_RATING_WEIGHT: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.ADMIN_AUTHOR_LINK: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.ADMIN_AUTHOR_MERGE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.ADMIN_PACKET_ACCESS: ActionPolicy(
            mutation=True, idempotency_required=True, sensitive_response=True
        ),
        ActionCode.PLAYER_UNBAN: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.BUG_REPORT_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.ADMIN_SUSPICION_CLEAR: ActionPolicy(
            mutation=True, idempotency_required=True
        ),
        ActionCode.NAVIGATION_CONTEXT_SET: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_JOIN: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.LOBBY_INVITE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_CREATE: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.LOBBY_LINK: ActionPolicy(mutation=True, idempotency_required=True),
        ActionCode.LOBBY_SETTINGS_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_READY_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_ROLE_UPDATE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_PACKET_SELECT: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_PACKET_SELECT_MANY: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_PACKET_REMOVE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_LEAVE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_CANCEL: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_START: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_SEARCH_START: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.LOBBY_SEARCH_CANCEL: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.REPUTATION_VOTE: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
        ActionCode.PLAYER_REPORT: ActionPolicy(
            mutation=True, idempotency_required=True, stale_write_field="expected_version"
        ),
    }
)


class ApplicationGateway:
    """Typed, transport-neutral entry point for bot and Mini App application calls."""

    CONTRACT_VERSION = "1.0"

    def __init__(
        self,
        database: Database,
        *,
        player_accounts: PlayerAccountService | None = None,
        author_links: AuthorLinkService | None = None,
        token_requests: TournamentTokenRequestService | None = None,
        tournaments: TournamentService | None = None,
        matchmaking: InvitationMatchmakingService | None = None,
        navigation: TelegramNavigationService | None = None,
        trust: TrustService | None = None,
        admin_authentication: PlatformAdminAuthenticationService | None = None,
        launch_references: LaunchReferenceService | None = None,
        packets: PacketAdminService | None = None,
        minimum_client_version: str = "1.0.0",
        idempotency_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        self.database = database
        self.player_accounts = player_accounts or PlayerAccountService(database)
        self.author_links = author_links or AuthorLinkService(database)
        self.token_requests = token_requests
        self.tournaments = tournaments or TournamentService(database)
        self.matchmaking = matchmaking or InvitationMatchmakingService(database)
        self.navigation = navigation or TelegramNavigationService(
            database, player_accounts=self.player_accounts
        )
        self.trust = trust or TrustService(database)
        self.moderation = PlayerModerationService(database)
        self.bug_reports = BugReportService(database)
        from sitg_bot.services.telegram_game import TelegramGameService

        self.telegram_games = TelegramGameService(database)
        from sitg_bot.services.chat import ParticipantChatService

        self.chat = ParticipantChatService(database)
        from sitg_bot.services.tournament_chats import TournamentChatService

        self.tournament_chats = TournamentChatService(database)
        self.admin_authentication = admin_authentication
        self.launch_references = launch_references
        self.packets = packets or PacketAdminService(database)
        self.library = PacketLibraryService(database)
        self.profiles = PlayerProfileService(database)
        self.tournament_profiles = TournamentProfileService(database)
        self.minimum_client_version = minimum_client_version
        self.idempotency_lease = idempotency_lease

    async def execute(
        self, principal: ApplicationPrincipal, request: GatewayRequest
    ) -> GatewayResponse:
        action = ActionCode(request.operation.action)
        policy = ACTION_POLICIES[action]
        key_digest = self._key_digest(request.metadata.idempotency_key)
        idempotency: ApplicationIdempotencyRecord | None = None
        audit_id: UUID | None = None
        try:
            audit_id = await self._start_audit(principal, request, key_digest=key_digest)
            if policy.authentication_required:
                self._require_telegram_principal(principal)
            if policy.idempotency_required and request.metadata.idempotency_key is None:
                raise _IdempotencyRequired
            if policy.idempotency_required:
                cached, idempotency = await self._begin_idempotency(
                    principal, request, policy=policy, key_digest=key_digest
                )
                if cached is not None:
                    cached_error_code = cached.error.code.value if cached.error else None
                    cached_outcome = "succeeded"
                    if cached.error is not None:
                        cached_outcome = (
                            "failed"
                            if cached.error.code == ErrorCode.INTERNAL_ERROR
                            else "rejected"
                        )
                    await self._finish_audit(
                        audit_id,
                        outcome=cached_outcome,
                        error_code=cached_error_code,
                    )
                    return cached.model_copy(
                        update={"correlation_id": request.metadata.correlation_id}
                    )

            data = await self._dispatch(principal, request)
            response = GatewayResponse(
                action=action,
                correlation_id=request.metadata.correlation_id,
                ok=True,
                data=self._json_value(data),
            )
            if idempotency is not None:
                await self._finish_idempotency(
                    idempotency.id,
                    response,
                    store_response=not policy.sensitive_response,
                )
            await self._finish_audit(audit_id, outcome="succeeded", error_code=None)
            return response
        except Exception as error:
            gateway_error = self._error(error, action=action)
            if gateway_error.code == ErrorCode.INTERNAL_ERROR:
                LOGGER.exception(
                    "Unhandled application gateway error action=%s correlation=%s",
                    action.value,
                    request.metadata.correlation_id,
                )
            response = GatewayResponse(
                action=action,
                correlation_id=request.metadata.correlation_id,
                ok=False,
                error=gateway_error,
            )
            if idempotency is not None:
                await self._finish_idempotency(idempotency.id, response, store_response=True)
            outcome = "failed" if gateway_error.code == ErrorCode.INTERNAL_ERROR else "rejected"
            if audit_id is not None:
                await self._finish_audit(
                    audit_id, outcome=outcome, error_code=gateway_error.code.value
                )
            return response

    async def _dispatch(self, principal: ApplicationPrincipal, request: GatewayRequest) -> object:
        operation = request.operation
        action = ActionCode(operation.action)
        if action == ActionCode.CAPABILITIES:
            return self.capabilities()

        if principal.telegram_user_id is None and principal.player_id is None:
            if isinstance(operation, PlayerListOperation):
                return await self.profiles.list_players(**operation.model_dump(exclude={"action"}))
            if isinstance(operation, PlayerProfileOperation):
                return await self.profiles.profile(
                    None, operation.player_id, ruleset_key=operation.ruleset_key
                )
            if isinstance(operation, PlayerGameResultsOperation):
                return await self.profiles.game_results(
                    None, operation.player_id, operation.game_id
                )
            if isinstance(operation, TournamentListOperation):
                return await self.tournaments.list_visible(
                    None, **operation.model_dump(exclude={"action"})
                )
            if isinstance(operation, TournamentInfoOperation):
                return await self.tournaments.tournament_details(
                    operation.tournament_id, None, role=operation.role
                )
            if isinstance(operation, TournamentProfileOperation):
                return await self.tournament_profiles.profile(
                    None, operation.tournament_id
                )

        telegram_user_id = self._require_telegram_principal(principal)
        if principal.player_id is not None:
            claimed_account = await self.player_accounts.lookup_by_telegram_user_id(
                telegram_user_id
            )
            self._verify_claimed_player(
                principal, claimed_account.player_id if claimed_account else None
            )
        if isinstance(operation, AccountLookupOperation):
            account = await self.player_accounts.lookup_by_telegram_user_id(telegram_user_id)
            self._verify_claimed_player(principal, account.player_id if account else None)
            return account
        if isinstance(operation, RegistrationStartOperation):
            draft = await self.player_accounts.create_or_resume_registration(
                telegram_user_id, telegram_username=operation.telegram_username
            )
            self._verify_claimed_player(principal, draft.account.player_id)
            return draft
        if isinstance(operation, RegistrationStepSaveOperation):
            draft = await self.player_accounts.save_registration_step(
                telegram_user_id,
                step=operation.step,
                value=operation.value,
                expected_version=operation.expected_version,
            )
            self._verify_claimed_player(principal, draft.account.player_id)
            return draft
        if isinstance(operation, RegistrationCompleteOperation):
            account = await self.player_accounts.complete_registration(
                telegram_user_id, expected_version=operation.expected_version
            )
            self._verify_claimed_player(principal, account.player_id)
            return account
        if isinstance(operation, TelegramUsernameRefreshOperation):
            account = await self.player_accounts.refresh_telegram_username(
                telegram_user_id, operation.telegram_username
            )
            self._verify_claimed_player(principal, account.player_id)
            return account
        if isinstance(operation, NavigationSnapshotOperation):
            snapshot = await self.navigation.snapshot(telegram_user_id)
            self._verify_claimed_player(
                principal, snapshot.account.player_id if snapshot is not None else None
            )
            return snapshot
        if isinstance(operation, SettingsUpdateOperation) and operation.key == "language":
            return await self.player_accounts.update_setting(
                telegram_user_id,
                key=operation.key,
                value=operation.value,
                expected_version=operation.expected_version,
            )

        player_id = await self._require_active_principal(principal)
        await self._reject_banned_player(player_id, action)
        if isinstance(operation, PlayerListOperation):
            return await self.profiles.list_players(**operation.model_dump(exclude={"action"}))
        if isinstance(operation, PlayerProfileOperation):
            return await self.profiles.profile(
                player_id,
                operation.player_id,
                ruleset_key=operation.ruleset_key,
            )
        if isinstance(operation, PlayerGameResultsOperation):
            return await self.profiles.game_results(
                player_id, operation.player_id, operation.game_id
            )
        if isinstance(operation, PlayerResolveOperation):
            return await self.profiles.resolve_reference(operation.reference)
        if isinstance(operation, AdminAuthenticateOperation):
            await self._admin_authenticator().authenticate(
                player_id,
                telegram_user_id,
                operation.credential,
            )
            return await self.navigation.snapshot(telegram_user_id)
        if isinstance(operation, NavigationModeSetOperation):
            return await self.navigation.set_mode(
                telegram_user_id,
                operation.mode,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, NavigationTournamentSetOperation):
            return await self.navigation.select_tournament(
                telegram_user_id,
                mode=operation.mode,
                tournament_id=operation.tournament_id,
                expected_version=operation.expected_version,
            )
        if action == ActionCode.SETTINGS_LIST:
            return await self.player_accounts.read_settings(telegram_user_id)
        if isinstance(operation, SettingsUpdateOperation):
            return await self.player_accounts.update_setting(
                telegram_user_id,
                key=operation.key,
                value=operation.value,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, AuthorsSearchOperation):
            return await self.author_links.search_authors(
                operation.query, cursor=operation.cursor, limit=operation.limit
            )
        if isinstance(operation, AuthorLinkCreateOperation):
            return await self.author_links.create_request(
                player_id, operation.author_id, note=operation.note
            )
        if isinstance(operation, AuthorLinkCancelOperation):
            return await self.author_links.cancel_request(operation.request_id, player_id)
        if isinstance(operation, AuthorLinkMineOperation):
            return await self.author_links.player_requests(
                player_id, cursor=operation.cursor, limit=operation.limit
            )
        if isinstance(operation, AuthorLinkAdminPendingOperation):
            return await self.author_links.admin_queue(
                player_id, cursor=operation.cursor, limit=operation.limit
            )
        if isinstance(operation, AuthorLinkAdminDecideOperation):
            return await self.author_links.decide_request(
                operation.request_id,
                player_id,
                approve=operation.approve,
                note=operation.note,
            )
        if isinstance(operation, TokenRequestCreateOperation):
            return await self._tokens().create_request(
                player_id,
                tournament_name=operation.tournament_name,
                justification=operation.justification,
            )
        if isinstance(operation, TokenRequestQueueOperation):
            method = (
                self._tokens().pending_queue
                if action == ActionCode.TOKEN_REQUEST_ADMIN_PENDING
                else self._tokens().resolved_queue
            )
            return await method(player_id, cursor=operation.cursor, limit=operation.limit)
        if isinstance(operation, TokenRequestAdminDecideOperation):
            lifetime = (
                timedelta(seconds=operation.lifetime_seconds)
                if operation.lifetime_seconds is not None
                else None
            )
            return await self._tokens().decide_request(
                operation.request_id,
                player_id,
                approve=operation.approve,
                note=operation.note,
                lifetime=lifetime,
            )
        if action == ActionCode.TOKEN_INVENTORY:
            return await self._tokens().my_tokens_page(
                player_id, cursor=operation.cursor, limit=operation.limit
            )
        if isinstance(operation, TokenClaimOperation):
            return await self._tokens().claim_token_once(operation.request_id, player_id)
        if isinstance(operation, TournamentCreateOperation):
            return await self.tournaments.create_tournament(
                token_id=operation.token_id,
                creator_id=player_id,
                name=operation.name,
                slug=operation.slug,
                type_key=operation.type_key,
                game_ruleset_key=operation.game_ruleset_key,
                visibility=operation.visibility,
                language=operation.language,
                registration_ends_at=operation.registration_ends_at,
                starts_at=operation.starts_at,
                planned_ends_at=operation.planned_ends_at,
            )
        if isinstance(operation, TournamentListOperation):
            return await self.tournaments.list_visible(
                player_id,
                role=operation.role,
                include_managed_public=operation.include_managed_public,
                phase=operation.phase,
                relationship=operation.relationship,
                registration=operation.registration,
                type_key=operation.type_key,
                ruleset_key=operation.ruleset_key,
                language=operation.language,
                search=operation.search,
                order=operation.order,
                cursor=operation.cursor,
                limit=operation.limit,
            )
        if isinstance(operation, TournamentInfoOperation):
            return await self.tournaments.tournament_details(
                operation.tournament_id, player_id, role=operation.role
            )
        if isinstance(operation, TournamentProfileOperation):
            return await self.tournament_profiles.profile(player_id, operation.tournament_id)
        if isinstance(operation, TournamentRegisterOperation):
            return await self.tournaments.register(
                operation.tournament_id, player_id,
                invitation_reference=operation.invitation_reference,
            )
        if isinstance(operation, TournamentRegistrationLinkOperation):
            return await self.tournaments.registration_link(operation.tournament_id, player_id)
        if isinstance(operation, TournamentRegistrationInvitationOperation):
            return await self.tournaments.registration_invitation(operation.reference, player_id)
        if isinstance(operation, TournamentManagerSettingsLinkOperation):
            if self.launch_references is None:
                raise _CapabilityUnavailable
            tournament_id = await self._manager_tournament_id(telegram_user_id, None)
            return await self.launch_references.create(
                route="manager_settings",
                target_id=tournament_id,
                created_by_player_id=player_id,
                intended_player_id=player_id,
                one_time=False,
            )
        if isinstance(operation, TournamentManagerSettingsOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.manager_settings(tournament_id, player_id)
        if isinstance(operation, TournamentManagerSettingsUpdateOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.update_manager_settings(
                tournament_id,
                player_id,
                expected_version=operation.expected_version,
                name=operation.name,
                slug=operation.slug,
                type_key=operation.type_key,
                game_ruleset_key=operation.game_ruleset_key,
                visibility=operation.visibility,
                language=operation.language,
                payment_type=operation.payment_type,
                pricing_plans=operation.pricing_plans,
                registration_open=operation.registration_open,
                ignore_late_registrations=operation.ignore_late_registrations,
                registration_open_override=operation.registration_open_override,
                registration_starts_at=operation.registration_starts_at,
                registration_ends_at=operation.registration_ends_at,
                starts_at=operation.starts_at,
                planned_ends_at=operation.planned_ends_at,
                description=operation.description,
                author_names=operation.author_names,
                author_ids=operation.author_ids,
                default_parameters=dict(operation.default_parameters),
                player_mutable_parameters=operation.player_mutable_parameters,
                policies=dict(operation.policies),
            )
        if isinstance(operation, TournamentAuthorCreateOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            create = (
                self.tournaments.create_tournament_author if operation.return_author
                else self.tournaments.register_tournament_author
            )
            return await create(
                tournament_id,
                player_id,
                first_name=operation.first_name,
                second_name=operation.second_name,
                surname=operation.surname,
                telegram_link=operation.telegram_link,
            )
        if isinstance(operation, TournamentFinalizeOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.finalize_tournament_setup(
                tournament_id, player_id, expected_version=operation.expected_version
            )
        if isinstance(operation, TournamentManagerManagementLinkOperation):
            if self.launch_references is None:
                raise _CapabilityUnavailable
            tournament_id = await self._manager_tournament_id(telegram_user_id, None)
            return await self.launch_references.create(
                route="manager_management",
                target_id=tournament_id,
                created_by_player_id=player_id,
                intended_player_id=player_id,
                one_time=False,
            )
        if isinstance(operation, TournamentManagerManagementOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.manager_management(tournament_id, player_id)
        if isinstance(operation, TournamentClassicUpdateOperation):
            from sitg_bot.services.classic import ClassicService

            await ClassicService(self.tournaments.database).mutate(
                operation.tournament_id,
                player_id,
                expected_version=operation.expected_version,
                command=operation.command,
                kind=operation.kind,
                values=dict(operation.values),
            )
            return await self.tournaments.manager_management(operation.tournament_id, player_id)
        if isinstance(operation, TournamentRegistrationOverrideOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.set_registration_override(
                tournament_id,
                player_id,
                registration_open=operation.registration_open,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, TournamentRegistrationDecideOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            if operation.decision == "approve":
                await self.tournaments.approve_registration(
                    tournament_id, operation.player_id, manager_id=player_id
                )
            else:
                await self.tournaments.reject_registration(
                    tournament_id, operation.player_id, manager_id=player_id
                )
            return await self.tournaments.manager_management(tournament_id, player_id)
        if isinstance(operation, TournamentPacketAccessUpdateOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.set_management_packet_access(
                tournament_id,
                operation.assignment_id,
                player_id,
                right=operation.right,
                enabled=operation.enabled,
                player_id=operation.player_id,
                library_viewing_rule=operation.library_viewing_rule,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, TournamentCompleteOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            await self.tournaments.complete_tournament(
                tournament_id,
                player_id,
                expected_version=operation.expected_version,
            )
            return await self.tournaments.manager_management(tournament_id, player_id)
        if isinstance(operation, TournamentStartOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.tournaments.start_tournament(
                tournament_id, player_id, expected_version=operation.expected_version,
            )
        if isinstance(operation, PacketUploadEligibilityOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            return await self.packets.upload_eligibility(tournament_id, player_id)
        if isinstance(operation, LibraryListOperation):
            return await self.library.list_packets(player_id)
        if isinstance(operation, LibraryAccessOperation):
            return await self.library.access(
                player_id, operation.version_id, confirm=operation.confirm,
                download=action == ActionCode.LIBRARY_DOWNLOAD,
                request_key=hashlib.sha256(
                    (request.metadata.idempotency_key or "").encode()
                ).hexdigest(),
            )
        if isinstance(operation, PacketManagementGetOperation):
            return await self.packets.management_editor(
                operation.tournament_id, operation.assignment_id, player_id
            )
        if isinstance(operation, (PacketExistingPreviewOperation, PacketExistingAddOperation)):
            preview = await self.packets.existing_packet(
                operation.tournament_id, operation.packet_id, player_id,
                expected_version_id=(
                    operation.expected_version_id
                    if isinstance(operation, PacketExistingAddOperation) else None
                ),
            )
            if isinstance(operation, PacketExistingAddOperation):
                return await self.tournaments.manager_management(operation.tournament_id, player_id)
            return preview
        if isinstance(operation, PacketManagementUpdateOperation):
            await self.packets.modify_packet(
                operation.tournament_id, operation.assignment_id, player_id,
                expected_version=operation.expected_version,
                content=dict(operation.content), changes=operation.changes,
                field_author_ids=operation.field_author_ids,
            )
            return await self.tournaments.manager_management(operation.tournament_id, player_id)
        if isinstance(operation, PacketManagementActionOperation):
            await self.packets.management_action(
                operation.tournament_id, operation.assignment_id, player_id,
                expected_version=operation.expected_version,
                delete=action == ActionCode.PACKET_MANAGEMENT_DELETE,
            )
            return await self.tournaments.manager_management(operation.tournament_id, player_id)
        if isinstance(operation, PacketUploadOperation):
            tournament_id = await self._manager_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            try:
                source = base64.b64decode(operation.source_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("Packet upload is not valid base64") from error
            result = await self.packets.import_upload(
                source,
                source_filename=operation.source_filename,
                uploader_id=player_id,
                tournament_id=tournament_id,
            )
            summaries = result.get("drafts", [result])
            for summary in summaries:
                if self.launch_references is not None:
                    reference = await self.launch_references.create(
                        route="packet_draft",
                        target_id=UUID(str(summary["draft_id"])),
                        created_by_player_id=player_id,
                        intended_player_id=player_id,
                        lifetime=timedelta(hours=2),
                        one_time=False,
                    )
                    summary.update(
                        launch_reference=reference.value,
                        launch_expires_at=reference.expires_at,
                    )
            return result
        if isinstance(operation, PacketDraftGetOperation):
            return await self.packets.editable_draft(operation.draft_id, player_id)
        if isinstance(operation, PacketDraftUpdateOperation):
            return await self.packets.update_draft(
                operation.draft_id,
                player_id,
                expected_version=operation.expected_version,
                content=dict(operation.content),
                author_bindings=operation.author_bindings,
                lead_author_id=operation.lead_author_id,
            )
        if isinstance(operation, PacketDraftAuthorCreateOperation):
            return await self.packets.create_author(
                operation.draft_id,
                player_id,
                first_name=operation.first_name,
                second_name=operation.second_name,
                surname=operation.surname,
                telegram_link=operation.telegram_link,
            )
        if isinstance(operation, PacketDraftTelegramBindOperation):
            await self.packets.bind_telegram_message(
                operation.draft_id,
                player_id,
                chat_id=operation.chat_id,
                message_id=operation.message_id,
                locale=operation.locale,
            )
            return {"draft_id": str(operation.draft_id), "bound": True}
        if isinstance(operation, PacketDraftDecisionOperation):
            summary = await self.packets.draft_summary(operation.draft_id, player_id)
            if summary["status"] in {"published", "rejected"}:
                return summary
            notify_telegram = request.metadata.channel == "mini_app"
            if action == ActionCode.PACKET_DRAFT_REJECT:
                await self.packets.reject(
                    operation.draft_id,
                    actor_id=player_id,
                    notify_bound_telegram=notify_telegram,
                )
                return await self.packets.draft_summary(operation.draft_id, player_id)
            packet = await self.packets.publish(
                operation.draft_id,
                administrator_id=player_id,
                notify_bound_telegram=notify_telegram,
            )
            return {
                "draft_id": str(operation.draft_id),
                "status": "published",
                "packet_id": str(packet.logical_id),
                "packet_version_id": str(packet.version_id),
                "name": packet.packet.name,
            }
        if isinstance(operation, NotificationsListOperation):
            return await self.author_links.notification_page(
                player_id,
                audience=operation.audience,
                read_state=operation.read_state,
                cursor=operation.cursor,
                limit=operation.limit,
            )
        if isinstance(operation, NotificationReadOperation):
            return await self.author_links.mark_notification_read(
                operation.notification_id, player_id
            )
        if isinstance(operation, NotificationsReadAllOperation):
            return await self.author_links.mark_notifications_read(
                player_id, audience=operation.audience
            )
        if isinstance(operation, NotificationAlertsClaimOperation):
            return await self.author_links.claim_notification_alerts(
                player_id, audience=operation.audience
            )
        if isinstance(operation, NavigationContextSetOperation):
            return await self.navigation.set_context(
                telegram_user_id, operation.context, expected_version=operation.expected_version
            )
        if isinstance(operation, LobbyJoinOperation):
            account = await self.player_accounts.lookup_by_telegram_user_id(telegram_user_id)
            lobby = await self.matchmaking.join(
                operation.invitation_code,
                ParticipantInput(
                    telegram_user_id=telegram_user_id, public_nickname=account.public_nickname
                ),
                role=operation.role,
                confirm_fresh=operation.confirm_fresh,
            )
            await self.navigation.set_context(telegram_user_id, "lobby")
            return lobby
        if isinstance(operation, LobbyInviteOperation):
            return await self.matchmaking.invite(
                operation.lobby_id,
                telegram_user_id,
                operation.username,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbyCreateOperation):
            tournament_id = await self._player_tournament_id(
                telegram_user_id, operation.tournament_id
            )
            account = await self.player_accounts.lookup_by_telegram_user_id(telegram_user_id)
            if account is None or account.public_nickname is None:
                raise _AuthenticationRequired
            lobby = await self.matchmaking.create_lobby(
                ParticipantInput(
                    telegram_user_id=telegram_user_id,
                    public_nickname=account.public_nickname,
                ),
                tournament_id=tournament_id,
                max_players=operation.max_players,
            )
            await self.navigation.set_context(telegram_user_id, "lobby")
            reference = await self._lobby_reference(lobby.id, player_id)
            async with self.database.transaction() as session:
                await TransactionalOutbox.enqueue(
                    session,
                    topic="telegram.lobby.open",
                    deduplication_key=f"lobby:{lobby.id}:open:chat:{telegram_user_id}",
                    partition_key=f"telegram:chat:{telegram_user_id}",
                    aggregate_type="lobby",
                    aggregate_id=lobby.id,
                    aggregate_sequence=1,
                    payload={
                        "recipient_telegram_user_id": telegram_user_id,
                        "locale": account.preferred_locale,
                        "launch_reference": reference.value,
                        "expires_at": reference.expires_at.isoformat(),
                    },
                )
            return {"lobby": lobby, "launch_reference": reference}
        if isinstance(operation, LobbyLinkOperation):
            await self._require_lobby_member(operation.lobby_id, player_id)
            return await self._lobby_reference(operation.lobby_id, player_id)
        if isinstance(operation, LobbyInfoOperation):
            await self._require_lobby_member(operation.lobby_id, player_id)
            return await self._lobby_payload(operation.lobby_id, telegram_user_id)
        if isinstance(operation, LobbyEventsOperation):
            await self._require_lobby_member(operation.lobby_id, player_id)
            async with self.database.sessions() as session:
                events = list(
                    (
                        await session.execute(
                            select(PregameLobbyEventRecord)
                            .where(
                                PregameLobbyEventRecord.lobby_id == operation.lobby_id,
                                PregameLobbyEventRecord.sequence > operation.after_sequence,
                            )
                            .order_by(PregameLobbyEventRecord.sequence)
                            .limit(operation.limit)
                        )
                    ).scalars()
                )
            return {
                "items": [
                    {
                        "sequence": event.sequence,
                        "kind": event.kind,
                        "created_at": event.created_at,
                    }
                    for event in events
                ]
            }
        if isinstance(operation, LobbySettingsUpdateOperation):
            return await self.matchmaking.set_settings(
                operation.lobby_id,
                telegram_user_id,
                operation.changes,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbyReadyUpdateOperation):
            return await self.matchmaking.set_ready(
                operation.lobby_id,
                telegram_user_id,
                ready=operation.ready,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbyRoleUpdateOperation):
            return await self.matchmaking.set_role(
                operation.lobby_id,
                telegram_user_id,
                operation.role,
                confirm_fresh=operation.confirm_fresh,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbyPacketBulkSelectOperation):
            return await self.matchmaking.select_packets(
                operation.lobby_id,
                telegram_user_id,
                operation.packet_ids,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbyPacketOperation):
            method = (
                self.matchmaking.select_packet
                if action == ActionCode.LOBBY_PACKET_SELECT
                else self.matchmaking.remove_packet
            )
            return await method(
                operation.lobby_id,
                telegram_user_id,
                operation.packet_id,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, LobbySimpleMutationOperation):
            methods = {
                ActionCode.LOBBY_LEAVE: self.matchmaking.leave,
                ActionCode.LOBBY_CANCEL: self.matchmaking.cancel,
                ActionCode.LOBBY_START: self.matchmaking.start,
                ActionCode.LOBBY_SEARCH_START: self.matchmaking.find_players,
                ActionCode.LOBBY_SEARCH_CANCEL: self.matchmaking.cancel_search,
            }
            return await methods[action](
                operation.lobby_id,
                telegram_user_id,
                expected_version=operation.expected_version,
            )
        if isinstance(operation, (GameAppealTicketsOperation, GameAppealDecideOperation)):
            from sitg_bot.services.persistent_game import PersistentGameService

            games = PersistentGameService(self.database)
            if isinstance(operation, GameAppealTicketsOperation):
                return (await games.manager_appeal_tickets(player_id))[:20]
            result = await games.decide_escalated_appeal(
                operation.appeal_id, player_id, approve=operation.approve
            )
            return {"accepted": result.accepted, "appeal_id": operation.appeal_id}
        if isinstance(operation, GameViewOperation):
            return await self.telegram_games.view(
                telegram_user_id, operation.game_id, reconnect=operation.reconnect
            )
        if isinstance(operation, GameObserveOperation):
            if request.metadata.channel not in {"telegram_bot", "mini_app"}:
                raise PermissionError("Telegram identity is required to observe")
            return await self.telegram_games.observe(
                telegram_user_id, operation.game_id, confirm_fresh=operation.confirm_fresh
            )
        if isinstance(operation, OngoingListOperation):
            from sitg_bot.services.persistent_game import PersistentGameService

            games = PersistentGameService(self.database)
            return {
                "lobbies": await self.matchmaking.ongoing_lobbies(player_id),
                "games": await games.ongoing_games(player_id),
            }
        if isinstance(operation, GameActOperation):
            return await self.telegram_games.act(telegram_user_id, operation)
        if isinstance(operation, ChatMembersOperation):
            if request.metadata.channel != "telegram_bot":
                raise PermissionError("Telegram chat adapter required")
            if operation.scope == "tournament_chat":
                return await self.tournament_chats.members(telegram_user_id, operation)
            return await self.chat.members(telegram_user_id, operation)
        if isinstance(operation, ChatSendOperation):
            if request.metadata.channel != "telegram_bot":
                raise PermissionError("Telegram chat adapter required")
            if operation.scope == "tournament_chat":
                return await self.tournament_chats.send(telegram_user_id, operation)
            return await self.chat.send(telegram_user_id, operation)
        if isinstance(operation, TournamentChatListOperation):
            if request.metadata.channel != "telegram_bot":
                raise PermissionError("Telegram chat adapter required")
            return await self.tournament_chats.list_rooms(telegram_user_id, operation)
        if isinstance(operation, TournamentChatOpenOperation):
            if request.metadata.channel != "telegram_bot":
                raise PermissionError("Telegram chat adapter required")
            return await self.tournament_chats.open(telegram_user_id, operation)
        if isinstance(operation, TournamentChatQuitOperation):
            if request.metadata.channel != "telegram_bot":
                raise PermissionError("Telegram chat adapter required")
            return await self.tournament_chats.quit(telegram_user_id)
        if isinstance(operation, TournamentChatInfoOperation):
            return await self.tournament_chats.info(telegram_user_id, operation)
        if isinstance(operation, TournamentChatGameTimeOperation):
            return await self.tournament_chats.set_game_time(telegram_user_id, operation)
        if isinstance(operation, ReputationVoteOperation):
            return await self.trust.vote_reputation(
                operation.game_id,
                player_id,
                operation.target_telegram_user_id,
                operation.value,
                expected_game_version=operation.expected_version,
            )
        if isinstance(operation, PlayerReportOperation):
            return await self.trust.report_player(
                operation.game_id,
                player_id,
                operation.target_telegram_user_id,
                operation.kind,
                details=operation.details,
                expected_game_version=operation.expected_version,
            )
        if isinstance(operation, PlayerBanOperation):
            return await self.moderation.ban_player(
                player_id, operation.target, reason=operation.reason
            )
        if isinstance(operation, PlayerUnbanOperation):
            return await self.moderation.unban_player(player_id, operation.target)
        if isinstance(operation, BugReportCreateOperation):
            return await self.bug_reports.submit(player_id, operation.commentary)
        if isinstance(operation, AdminSuspicionLedgerOperation):
            return await self.trust.suspicion_ledger(player_id, limit=operation.limit)
        if isinstance(operation, AdminManagementListOperation):
            return await AdminManagementService(self.database).catalogue(
                player_id, operation.section
            )
        if isinstance(operation, AdminTournamentModerateOperation):
            return await AdminManagementService(self.database).moderate_tournament(
                player_id, operation.tournament_id, command=operation.command,
                expected_version=operation.expected_version, confirm=operation.confirm,
            )
        if isinstance(operation, AdminTournamentRatingWeightOperation):
            return await AdminManagementService(self.database).set_tournament_rating_weight(
                player_id, operation.tournament_id,
                weight=operation.weight, expected_version=operation.expected_version,
            )
        if isinstance(operation, AdminAuthorLinkOperation):
            return await AdminManagementService(self.database).link_author(
                player_id, operation.author_id, operation.target
            )
        if isinstance(operation, AdminAuthorMergeOperation):
            return await AdminManagementService(self.database).merge_authors(
                player_id,
                operation.author_id,
                operation.merge_author_id,
                confirm=operation.confirm,
            )
        if isinstance(operation, AdminPacketAccessOperation):
            return await self.library.access(
                player_id, operation.version_id, confirm=operation.confirm,
                download=operation.command == "download", administrator=True,
                request_key=hashlib.sha256(
                    (request.metadata.idempotency_key or "").encode()
                ).hexdigest(),
            )
        if isinstance(operation, AdminSuspicionInspectOperation):
            return await self.trust.suspicion_inspection(
                player_id, operation.player_id, limit=operation.limit
            )
        if isinstance(operation, AdminSuspicionClearOperation):
            return await self.trust.clear_player_suspicion(
                player_id, operation.player_id, note=operation.note
            )
        raise _CapabilityUnavailable

    def capabilities(self) -> CapabilityPayload:
        actions = tuple(
            CapabilityAction(
                action=action,
                authentication_required=policy.authentication_required,
                mutation=policy.mutation,
                idempotency_required=policy.idempotency_required,
                stale_write_field=policy.stale_write_field,
                cursor_paginated=policy.cursor_paginated,
                sensitive_response=policy.sensitive_response,
            )
            for action, policy in sorted(ACTION_POLICIES.items(), key=lambda item: item[0].value)
            if (
                self.token_requests is not None or not action.value.startswith("tournament_tokens.")
            )
            and (self.admin_authentication is not None or action != ActionCode.ADMIN_AUTHENTICATE)
            and (
                self.launch_references is not None
                or action
                not in {
                    ActionCode.TOURNAMENT_MANAGER_MANAGEMENT_LINK,
                    ActionCode.TOURNAMENT_MANAGER_SETTINGS_LINK,
                    ActionCode.LOBBY_CREATE,
                    ActionCode.LOBBY_LINK,
                }
            )
        )
        return CapabilityPayload(
            minimum_client_version=self.minimum_client_version,
            actions=actions,
            features=(
                "stable_errors",
                "durable_idempotency",
                "request_audit",
                "optimistic_concurrency",
                "cursor_pagination",
                "authoritative_telegram_navigation",
                "tournament_catalogue",
                "player_tournament_lobbies",
                "telegram_gameplay",
                "tournament_chats",
                "exclusive_backend_process",
                "transactional_outbox",
                "durable_jobs",
            ),
        )

    async def _require_active_principal(self, principal: ApplicationPrincipal) -> UUID:
        telegram_user_id = self._require_telegram_principal(principal)
        account = await self.player_accounts.lookup_by_telegram_user_id(telegram_user_id)
        if account is None:
            raise _AuthenticationRequired
        self._verify_claimed_player(principal, account.player_id)
        if account.registration_status != "active":
            raise _AuthenticationRequired
        return account.player_id

    async def _reject_banned_player(self, player_id: UUID, action: ActionCode) -> None:
        if action in BAN_EXEMPT_ACTIONS:
            return
        async with self.database.transaction() as session:
            reason = await PlayerModerationService.active_ban_reason(session, player_id)
        if reason is not None:
            raise PermissionError("Banned players can only use their library")

    async def _require_lobby_member(self, lobby_id: UUID, player_id: UUID) -> None:
        async with self.database.transaction() as session:
            membership = await session.scalar(
                select(PregameLobbyMemberRecord.id).where(
                    PregameLobbyMemberRecord.lobby_id == lobby_id,
                    PregameLobbyMemberRecord.player_id == player_id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
            )
            if membership is None:
                raise PermissionError("Active lobby membership is required")

    async def _manager_tournament_id(
        self, telegram_user_id: int, tournament_id: UUID | None
    ) -> UUID:
        if tournament_id is not None:
            return tournament_id
        navigation = await self.navigation.snapshot(telegram_user_id)
        if navigation is None or navigation.selected_manager_tournament is None:
            raise LookupError("No manager tournament is selected")
        return navigation.selected_manager_tournament.id

    async def _player_tournament_id(
        self, telegram_user_id: int, tournament_id: UUID | None
    ) -> UUID:
        navigation = await self.navigation.snapshot(telegram_user_id)
        selected = navigation.selected_player_tournament if navigation is not None else None
        if selected is None:
            raise LookupError("No player tournament is selected")
        if tournament_id is not None and tournament_id != selected.id:
            raise PermissionError("The selected player tournament has changed")
        return selected.id

    async def _lobby_reference(self, lobby_id: UUID, player_id: UUID) -> object:
        if self.launch_references is None:
            raise _CapabilityUnavailable
        async with self.database.sessions() as session:
            lobby = await session.get(PregameLobbyRecord, lobby_id)
            if lobby is None or lobby.status != "assembling":
                raise LookupError("Active lobby not found")
            lifetime = min(lobby.expires_at - datetime.now(UTC), timedelta(hours=24))
            if lifetime <= timedelta(0):
                raise LookupError("Active lobby not found")
        return await self.launch_references.create(
            route="lobby",
            target_id=lobby_id,
            created_by_player_id=player_id,
            intended_player_id=player_id,
            lifetime=lifetime,
            one_time=False,
        )

    async def _lobby_payload(self, lobby_id: UUID, telegram_user_id: int) -> dict[str, object]:
        lobby = await self.matchmaking.get(lobby_id)
        suggestions = await self.matchmaking.suggest_packets(
            lobby_id, telegram_user_id=telegram_user_id
        )
        viewer = next(
            (member for member in lobby.members if member.telegram_user_id == telegram_user_id),
            None,
        )
        if viewer is None:
            raise PermissionError("Active lobby membership is required")
        async with self.database.sessions() as session:
            record = await session.get(PregameLobbyRecord, lobby_id)
            creator_telegram_id = (
                await session.scalar(
                    select(PlayerRecord.telegram_user_id).where(
                        PlayerRecord.id == record.creator_player_id
                    )
                )
                if record is not None
                else None
            )
            last_event_sequence = int(
                await session.scalar(
                    select(func.max(PregameLobbyEventRecord.sequence)).where(
                        PregameLobbyEventRecord.lobby_id == lobby_id
                    )
                )
                or 0
            )
        is_creator = creator_telegram_id == telegram_user_id
        mutable_parameters: list[str] = []
        async with self.database.sessions() as session:
            context = await self.tournaments.context(session, lobby.tournament_id)
            policy = await session.get(
                TournamentPolicyVersionRecord, record.tournament_policy_version_id
            )
            mutable_parameters = sorted(policy.player_mutable_parameters)
            tournament = await session.get(TournamentRecord, lobby.tournament_id)
            tournament_name = tournament.name
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            descriptors = [
                {
                    "name": definition.name,
                    "value_type": definition.value_type,
                    "description_key": definition.description_key,
                    "value": lobby.settings.to_dict().get(definition.name),
                    "options": [],
                }
                for definition in ruleset.parameter_definitions
            ]
        actions = ["refresh"]
        if lobby.status == "assembling":
            if viewer.role != "player":
                actions.append("role_player")
            if viewer.role != "observer" and context.observing_policy != "forbidden":
                actions.append("role_observer")
            if viewer.role == "player":
                actions.append("unready" if viewer.ready else "ready")
            actions.extend(("invite", "cancel" if is_creator else "leave"))
            if is_creator:
                actions.extend(("settings_update", "packet_select", "packet_remove", "start"))
                if lobby.hybrid_matchmaking_available:
                    actions.append("search_cancel" if lobby.searching else "search_start")
        return {
            **asdict(lobby),
            "viewer": asdict(viewer),
            "available_actions": actions,
            "mutable_parameters": mutable_parameters,
            "setting_descriptors": descriptors,
            "tournament_name": tournament_name,
            "observing_policy": context.observing_policy,
            "packet_suggestions": suggestions,
            "last_event_sequence": last_event_sequence,
            "poll_after_seconds": 5,
        }

    def _tokens(self) -> TournamentTokenRequestService:
        if self.token_requests is None:
            raise _CapabilityUnavailable
        return self.token_requests

    def _admin_authenticator(self) -> PlatformAdminAuthenticationService:
        if self.admin_authentication is None:
            raise _CapabilityUnavailable
        return self.admin_authentication

    @staticmethod
    def _require_telegram_principal(principal: ApplicationPrincipal) -> int:
        if principal.telegram_user_id is None:
            raise _AuthenticationRequired
        return principal.telegram_user_id

    @staticmethod
    def _verify_claimed_player(
        principal: ApplicationPrincipal, resolved_player_id: UUID | None
    ) -> None:
        if principal.player_id is not None and principal.player_id != resolved_player_id:
            raise PermissionError("Authenticated identity does not match the account")

    async def _start_audit(
        self,
        principal: ApplicationPrincipal,
        request: GatewayRequest,
        *,
        key_digest: str | None,
    ) -> UUID:
        async with self.database.transaction() as session:
            record = ApplicationRequestAuditRecord(
                correlation_id=request.metadata.correlation_id,
                actor_player_id=principal.player_id,
                channel=request.metadata.channel,
                action=ActionCode(request.operation.action).value,
                client_name=request.metadata.client_name,
                client_version=request.metadata.client_version,
                idempotency_key_digest=key_digest,
                resource_metadata=self._resource_metadata(request),
                outcome="started",
            )
            session.add(record)
            await session.flush()
            return record.id

    async def _finish_audit(self, audit_id: UUID, *, outcome: str, error_code: str | None) -> None:
        async with self.database.transaction() as session:
            record = await session.get(
                ApplicationRequestAuditRecord, audit_id, with_for_update=True
            )
            if record is not None:
                record.outcome = outcome
                record.error_code = error_code
                record.completed_at = datetime.now(UTC)

    async def _begin_idempotency(
        self,
        principal: ApplicationPrincipal,
        request: GatewayRequest,
        *,
        policy: ActionPolicy,
        key_digest: str | None,
    ) -> tuple[GatewayResponse | None, ApplicationIdempotencyRecord | None]:
        assert key_digest is not None and request.metadata.idempotency_key is not None
        telegram_user_id = self._require_telegram_principal(principal)
        actor_scope = hashlib.sha256(f"telegram:{telegram_user_id}".encode()).hexdigest()
        request_digest = self._request_digest(request)
        action = ActionCode(request.operation.action).value
        lock_key = self._advisory_key(actor_scope, request.metadata.channel, action, key_digest)
        async with self.database.transaction() as session:
            await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            record = await session.scalar(
                select(ApplicationIdempotencyRecord)
                .where(
                    ApplicationIdempotencyRecord.actor_scope == actor_scope,
                    ApplicationIdempotencyRecord.channel == request.metadata.channel,
                    ApplicationIdempotencyRecord.action == action,
                    ApplicationIdempotencyRecord.key_digest == key_digest,
                )
                .with_for_update()
            )
            now = datetime.now(UTC)
            if record is not None:
                if record.request_digest != request_digest:
                    raise _IdempotencyConflict
                if record.status == "failed":
                    if record.response_payload is not None:
                        return GatewayResponse.model_validate(record.response_payload), record
                    raise _RequestIndeterminate
                if record.status == "completed":
                    if policy.sensitive_response:
                        if request.operation.action == ActionCode.TOKEN_CLAIM:
                            raise _SecretAlreadyDelivered
                        return None, record
                    if record.response_payload is not None:
                        return GatewayResponse.model_validate(record.response_payload), record
                    raise _RequestIndeterminate
                if record.lease_expires_at <= now:
                    raise _RequestIndeterminate
                raise _RequestInProgress
            record = ApplicationIdempotencyRecord(
                actor_scope=actor_scope,
                channel=request.metadata.channel,
                action=action,
                key_digest=key_digest,
                request_digest=request_digest,
                correlation_id=request.metadata.correlation_id,
                lease_expires_at=now + self.idempotency_lease,
                status="pending",
            )
            session.add(record)
            await session.flush()
            return None, record

    async def _finish_idempotency(
        self,
        record_id: UUID,
        response: GatewayResponse,
        *,
        store_response: bool,
    ) -> None:
        async with self.database.transaction() as session:
            record = await session.get(
                ApplicationIdempotencyRecord, record_id, with_for_update=True
            )
            if record is None:
                return
            record.status = "completed" if response.ok else "failed"
            record.response_payload = (
                response.model_dump(mode="json") if store_response else {"completed": True}
            )
            record.error_code = response.error.code.value if response.error else None
            record.completed_at = datetime.now(UTC)

    @staticmethod
    def _error(error: Exception, *, action: ActionCode) -> GatewayError:
        if isinstance(error, LobbyReadinessError):
            known_reasons = {
                "closed",
                "expired",
                "observer",
                "packet_required",
                "packet_not_playable",
                "packet_content_incompatible",
                "insufficient_fresh_content",
                "ruleset_player_limit_exceeded",
                "tournament_stage_closed",
                "tournament_capacity_restriction",
                "tournament_packet_limit_exceeded",
                "tournament_membership_required",
                "classic_participants_required",
            }
            reason = error.reason if error.reason in known_reasons else "invalid"
            return GatewayError(
                code=ErrorCode.VALIDATION_FAILED,
                message_key=f"lobby.readiness.{reason}",
                details={
                    key: value
                    for key, value in error.details.items()
                    if key in {"required", "available", "minimum", "maximum", "actual"}
                    and isinstance(value, int)
                },
            )
        if isinstance(error, _AuthenticationRequired):
            code = ErrorCode.AUTHENTICATION_REQUIRED
        elif isinstance(error, _IdempotencyRequired):
            code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
        elif isinstance(error, _IdempotencyConflict):
            code = ErrorCode.IDEMPOTENCY_CONFLICT
        elif isinstance(error, _RequestInProgress):
            code = ErrorCode.REQUEST_IN_PROGRESS
        elif isinstance(error, _RequestIndeterminate):
            code = ErrorCode.REQUEST_INDETERMINATE
        elif isinstance(error, (_SecretAlreadyDelivered, TokenPlaintextUnavailable)):
            code = ErrorCode.SECRET_ALREADY_DELIVERED
        elif isinstance(error, _CapabilityUnavailable):
            code = ErrorCode.CAPABILITY_UNAVAILABLE
        elif isinstance(error, (ProfileVersionConflict, StaleWriteError)):
            code = ErrorCode.STALE_WRITE
        elif isinstance(error, PermissionError):
            code = ErrorCode.FORBIDDEN
        elif isinstance(error, LookupError):
            code = ErrorCode.NOT_FOUND
        elif isinstance(error, (ValueError, TypeError)):
            code = ErrorCode.VALIDATION_FAILED
        else:
            code = ErrorCode.INTERNAL_ERROR
        return GatewayError(
            code=code,
            message_key=f"error.{code.value}",
            retryable=code in {ErrorCode.REQUEST_IN_PROGRESS, ErrorCode.INTERNAL_ERROR},
        )

    @staticmethod
    def _resource_metadata(request: GatewayRequest) -> dict[str, str]:
        safe_keys = {
            "tournament_id",
            "lobby_id",
            "game_id",
            "player_id",
            "request_id",
            "author_id",
            "token_id",
            "notification_id",
            "appeal_id",
            "packet_id",
            "draft_id",
        }
        payload = request.operation.model_dump()
        return {
            key: str(value)
            for key, value in payload.items()
            if key in safe_keys and value is not None
        }

    @staticmethod
    def _request_digest(request: GatewayRequest) -> str:
        payload = request.operation.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _key_digest(key: str | None) -> str | None:
        return hashlib.sha256(key.encode()).hexdigest() if key is not None else None

    @staticmethod
    def _advisory_key(*parts: str) -> int:
        digest = hashlib.sha256("\0".join(parts).encode()).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    @classmethod
    def _json_value(cls, value: object) -> object:
        if isinstance(value, CapabilityPayload):
            return value.model_dump(mode="json")
        if is_dataclass(value) and not isinstance(value, type):
            return cls._json_value(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._json_value(item) for item in value]
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, Enum):
            return value.value
        if hasattr(value, "to_dict"):
            return cls._json_value(value.to_dict())  # type: ignore[union-attr]
        return value
