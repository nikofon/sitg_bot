from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    player_id: UUID | None = None
    telegram_user_id: int | None = None


class ActionCode(StrEnum):
    CAPABILITIES = "system.capabilities.v1"
    ADMIN_AUTHENTICATE = "platform.admin.authenticate.v1"
    ACCOUNT_LOOKUP = "player.account.lookup.v1"
    REGISTRATION_START = "player.registration.start.v1"
    REGISTRATION_STEP_SAVE = "player.registration.step.save.v1"
    REGISTRATION_COMPLETE = "player.registration.complete.v1"
    TELEGRAM_USERNAME_REFRESH = "player.telegram_username.refresh.v1"
    SETTINGS_LIST = "player.settings.list.v1"
    SETTINGS_UPDATE = "player.settings.update.v1"
    NAVIGATION_SNAPSHOT = "telegram.navigation.snapshot.v1"
    NAVIGATION_MODE_SET = "telegram.navigation.mode.set.v1"
    NAVIGATION_TOURNAMENT_SET = "telegram.navigation.tournament.set.v1"
    AUTHORS_SEARCH = "authors.search.v1"
    AUTHOR_LINK_CREATE = "author_links.create.v1"
    AUTHOR_LINK_CANCEL = "author_links.cancel.v1"
    AUTHOR_LINK_MINE = "author_links.mine.v1"
    AUTHOR_LINK_ADMIN_PENDING = "author_links.admin.pending.v1"
    AUTHOR_LINK_ADMIN_DECIDE = "author_links.admin.decide.v1"
    TOKEN_REQUEST_CREATE = "tournament_tokens.requests.create.v1"
    TOKEN_REQUEST_ADMIN_PENDING = "tournament_tokens.requests.admin.pending.v1"
    TOKEN_REQUEST_ADMIN_RESOLVED = "tournament_tokens.requests.admin.resolved.v1"
    TOKEN_REQUEST_ADMIN_DECIDE = "tournament_tokens.requests.admin.decide.v1"
    TOKEN_INVENTORY = "tournament_tokens.mine.v1"
    TOKEN_CLAIM = "tournament_tokens.claim.v1"
    TOURNAMENT_CREATE = "tournaments.create.v1"
    TOURNAMENT_LIST = "tournaments.list.v1"
    TOURNAMENT_INFO = "tournaments.info.v1"
    TOURNAMENT_PROFILE = "tournaments.profile.get.v1"
    TOURNAMENT_REGISTER = "tournaments.register.v1"
    TOURNAMENT_REGISTRATION_LINK = "tournaments.registration.link.v1"
    TOURNAMENT_REGISTRATION_INVITATION = "tournaments.registration.invitation.v1"
    TOURNAMENT_MANAGER_SETTINGS = "tournaments.manager.settings.v1"
    TOURNAMENT_MANAGER_SETTINGS_LINK = "tournaments.manager.settings.link.v1"
    TOURNAMENT_MANAGER_SETTINGS_UPDATE = "tournaments.manager.settings.update.v1"
    TOURNAMENT_MANAGER_MANAGEMENT = "tournaments.manager.management.v1"
    TOURNAMENT_MANAGER_MANAGEMENT_LINK = "tournaments.manager.management.link.v1"
    TOURNAMENT_REGISTRATION_OVERRIDE = "tournaments.manager.registration.override.v1"
    TOURNAMENT_CLASSIC_UPDATE = "tournaments.manager.classic.update.v1"
    TOURNAMENT_REGISTRATION_DECIDE = "tournaments.manager.registration.decide.v1"
    TOURNAMENT_PACKET_ACCESS_UPDATE = "tournaments.manager.packets.access.update.v1"
    PACKET_MANAGEMENT_GET = "packets.management.get.v1"
    LIBRARY_LIST = "library.list.v1"
    LIBRARY_VIEW = "library.view.v1"
    LIBRARY_DOWNLOAD = "library.download.v1"
    PLAYER_LIST = "players.list.v1"
    PLAYER_PROFILE = "players.profile.get.v1"
    PLAYER_GAME_RESULTS = "players.game_results.get.v1"
    PLAYER_RESOLVE = "players.resolve.v1"
    PACKET_MANAGEMENT_UPDATE = "packets.management.update.v1"
    PACKET_MANAGEMENT_DELETE = "packets.management.delete.v1"
    PACKET_MANAGEMENT_RELEASE = "packets.management.release.v1"
    TOURNAMENT_COMPLETE = "tournaments.manager.complete.v1"
    TOURNAMENT_START = "tournaments.manager.start.v1"
    PACKET_UPLOAD_ELIGIBILITY = "packets.upload.eligibility.v1"
    PACKET_UPLOAD = "packets.upload.v1"
    PACKET_DRAFT_GET = "packets.drafts.get.v1"
    PACKET_DRAFT_UPDATE = "packets.drafts.update.v1"
    PACKET_DRAFT_AUTHOR_CREATE = "packets.drafts.authors.create.v1"
    PACKET_DRAFT_TELEGRAM_BIND = "packets.drafts.telegram_message.bind.v1"
    PACKET_DRAFT_PUBLISH = "packets.drafts.publish.v1"
    PACKET_DRAFT_REJECT = "packets.drafts.reject.v1"
    TOURNAMENT_AUTHOR_CREATE = "tournaments.manager.authors.create.v1"
    TOURNAMENT_FINALIZE = "tournaments.finalize.v1"
    NOTIFICATIONS_LIST = "notifications.list.v1"
    NOTIFICATIONS_READ = "notifications.read.v1"
    NOTIFICATIONS_READ_ALL = "notifications.read_all.v1"
    NOTIFICATION_ALERTS_CLAIM = "notifications.alerts.claim.v1"
    NAVIGATION_CONTEXT_SET = "telegram.navigation.context.set.v1"
    LOBBY_JOIN = "lobbies.join.v1"
    LOBBY_INVITE = "lobbies.invite.v1"
    LOBBY_CREATE = "lobbies.create.v1"
    LOBBY_LINK = "lobbies.link.v1"
    LOBBY_INFO = "lobbies.info.v1"
    LOBBY_EVENTS = "lobbies.events.v1"
    LOBBY_SETTINGS_UPDATE = "lobbies.settings.update.v1"
    LOBBY_READY_UPDATE = "lobbies.ready.update.v1"
    LOBBY_ROLE_UPDATE = "lobbies.role.update.v1"
    LOBBY_PACKET_SELECT = "lobbies.packets.select.v1"
    LOBBY_PACKET_SELECT_MANY = "lobbies.packets.select_many.v1"
    LOBBY_PACKET_REMOVE = "lobbies.packets.remove.v1"
    LOBBY_LEAVE = "lobbies.leave.v1"
    LOBBY_CANCEL = "lobbies.cancel.v1"
    LOBBY_START = "lobbies.start.v1"
    LOBBY_SEARCH_START = "lobbies.search.start.v1"
    LOBBY_SEARCH_CANCEL = "lobbies.search.cancel.v1"
    REPUTATION_VOTE = "games.reputation.vote.v1"
    PLAYER_REPORT = "games.players.report.v1"
    GAME_VIEW = "games.view.v1"
    GAME_ACT = "games.act.v1"
    GAME_OBSERVE = "games.observe.join.v1"
    ONGOING_LIST = "ongoing.list.v1"
    CHAT_MEMBERS = "chat.members.v1"
    CHAT_SEND = "chat.send.v1"
    GAME_APPEAL_TICKETS = "games.appeals.manager.list.v1"
    GAME_APPEAL_DECIDE = "games.appeals.manager.decide.v1"
    PLAYER_BAN = "platform.players.ban.v1"
    PLAYER_UNBAN = "platform.players.unban.v1"
    BUG_REPORT_CREATE = "platform.bug_reports.create.v1"
    ADMIN_SUSPICION_LEDGER = "platform.admin.suspicion.ledger.v1"
    ADMIN_SUSPICION_INSPECT = "platform.admin.suspicion.inspect.v1"
    ADMIN_SUSPICION_CLEAR = "platform.admin.suspicion.clear.v1"
    ADMIN_MANAGEMENT_LIST = "platform.admin.management.list.v1"
    ADMIN_TOURNAMENT_MODERATE = "platform.admin.tournaments.moderate.v1"
    ADMIN_AUTHOR_LINK = "platform.admin.authors.link.v1"
    ADMIN_AUTHOR_MERGE = "platform.admin.authors.merge.v1"
    ADMIN_PACKET_ACCESS = "platform.admin.packets.access.v1"


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    STALE_WRITE = "stale_write"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    REQUEST_IN_PROGRESS = "request_in_progress"
    REQUEST_INDETERMINATE = "request_indeterminate"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    SECRET_ALREADY_DELIVERED = "secret_already_delivered"
    INTERNAL_ERROR = "internal_error"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestMetadata(ContractModel):
    correlation_id: UUID = Field(default_factory=uuid4)
    channel: Literal["console", "telegram_bot", "mini_app", "internal"]
    client_name: str = Field(min_length=1, max_length=80)
    client_version: str = Field(min_length=1, max_length=40)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200, repr=False)


class CapabilitiesOperation(ContractModel):
    action: Literal[ActionCode.CAPABILITIES]


class AdminAuthenticateOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_AUTHENTICATE]
    credential: str = Field(min_length=1, max_length=4096, repr=False)


class AccountLookupOperation(ContractModel):
    action: Literal[ActionCode.ACCOUNT_LOOKUP]


class RegistrationStartOperation(ContractModel):
    action: Literal[ActionCode.REGISTRATION_START]
    telegram_username: str | None = Field(default=None, max_length=32)


class RegistrationStepSaveOperation(ContractModel):
    action: Literal[ActionCode.REGISTRATION_STEP_SAVE]
    step: Literal["real_name", "nickname", "telegram_public"]
    value: JsonValue
    expected_version: int = Field(ge=0)


class RegistrationCompleteOperation(ContractModel):
    action: Literal[ActionCode.REGISTRATION_COMPLETE]
    expected_version: int = Field(ge=0)


class TelegramUsernameRefreshOperation(ContractModel):
    action: Literal[ActionCode.TELEGRAM_USERNAME_REFRESH]
    telegram_username: str | None = Field(default=None, max_length=32)


class SettingsListOperation(ContractModel):
    action: Literal[ActionCode.SETTINGS_LIST]


class SettingsUpdateOperation(ContractModel):
    action: Literal[ActionCode.SETTINGS_UPDATE]
    key: Literal["real_name", "nickname", "telegram_public", "language"]
    value: JsonValue
    expected_version: int = Field(ge=0)


class NavigationSnapshotOperation(ContractModel):
    action: Literal[ActionCode.NAVIGATION_SNAPSHOT]


class NavigationModeSetOperation(ContractModel):
    action: Literal[ActionCode.NAVIGATION_MODE_SET]
    mode: Literal["player", "manager", "admin"]
    expected_version: int = Field(ge=0)


class NavigationTournamentSetOperation(ContractModel):
    action: Literal[ActionCode.NAVIGATION_TOURNAMENT_SET]
    mode: Literal["player", "manager"]
    tournament_id: UUID | None
    expected_version: int = Field(ge=0)


class AuthorsSearchOperation(ContractModel):
    action: Literal[ActionCode.AUTHORS_SEARCH]
    query: str = Field(default="", max_length=300)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class AuthorLinkCreateOperation(ContractModel):
    action: Literal[ActionCode.AUTHOR_LINK_CREATE]
    author_id: UUID
    note: str | None = Field(default=None, max_length=1000)


class AuthorLinkCancelOperation(ContractModel):
    action: Literal[ActionCode.AUTHOR_LINK_CANCEL]
    request_id: UUID
    expected_status: Literal["pending"] = "pending"


class AuthorLinkMineOperation(ContractModel):
    action: Literal[ActionCode.AUTHOR_LINK_MINE]
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class AuthorLinkAdminPendingOperation(ContractModel):
    action: Literal[ActionCode.AUTHOR_LINK_ADMIN_PENDING]
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class AuthorLinkAdminDecideOperation(ContractModel):
    action: Literal[ActionCode.AUTHOR_LINK_ADMIN_DECIDE]
    request_id: UUID
    approve: bool
    note: str | None = Field(default=None, max_length=1000)
    expected_status: Literal["pending"] = "pending"


class TokenRequestCreateOperation(ContractModel):
    action: Literal[ActionCode.TOKEN_REQUEST_CREATE]
    tournament_name: str = Field(min_length=1, max_length=200)
    justification: str | None = Field(default=None, max_length=2000)


class TokenRequestQueueOperation(ContractModel):
    action: Literal[
        ActionCode.TOKEN_REQUEST_ADMIN_PENDING,
        ActionCode.TOKEN_REQUEST_ADMIN_RESOLVED,
    ]
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class TokenRequestAdminDecideOperation(ContractModel):
    action: Literal[ActionCode.TOKEN_REQUEST_ADMIN_DECIDE]
    request_id: UUID
    approve: bool
    note: str | None = Field(default=None, max_length=2000)
    lifetime_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    expected_status: Literal["pending"] = "pending"


class TokenInventoryOperation(ContractModel):
    action: Literal[ActionCode.TOKEN_INVENTORY]
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class TokenClaimOperation(ContractModel):
    action: Literal[ActionCode.TOKEN_CLAIM]
    request_id: UUID


class TournamentCreateOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_CREATE]
    token_id: UUID
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=200)
    type_key: str = Field(default="ladder", min_length=1, max_length=64)
    game_ruleset_key: str = Field(default="si", min_length=1, max_length=64)
    visibility: Literal["public", "private"] = "private"
    language: str = Field(default="und", min_length=1, max_length=35)
    registration_ends_at: datetime | None = None
    starts_at: datetime | None = None
    planned_ends_at: datetime | None = None


class TournamentListOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_LIST]
    role: Literal["player", "manager", "admin"] = "player"
    include_managed_public: bool = False
    phase: Literal["upcoming", "future", "ongoing", "past"] | None = None
    relationship: (
        Literal["discoverable", "registered", "approved", "participating", "managed"] | None
    ) = None
    registration: Literal["open", "closed"] | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=64)
    ruleset_key: str | None = Field(default=None, min_length=1, max_length=64)
    language: str | None = Field(default=None, min_length=1, max_length=35)
    search: str = Field(default="", max_length=200)
    order: Literal["starts_asc", "starts_desc", "name_asc", "name_desc"] = "starts_asc"
    cursor: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class TournamentInfoOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_INFO]
    tournament_id: UUID
    role: Literal["player", "manager", "admin"] = "player"


class TournamentProfileOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_PROFILE]
    tournament_id: UUID


class TournamentRegisterOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_REGISTER]
    tournament_id: UUID
    invitation_reference: str | None = Field(
        default=None, pattern=r"^(reg|join)_[A-Za-z0-9_-]{32}$",
    )


class TournamentRegistrationLinkOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_REGISTRATION_LINK]
    tournament_id: UUID


class TournamentRegistrationInvitationOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_REGISTRATION_INVITATION]
    reference: str = Field(pattern=r"^(reg|join)_[A-Za-z0-9_-]{32}$")


class TournamentManagerSettingsOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_MANAGER_SETTINGS]
    tournament_id: UUID | None = None


class TournamentManagerSettingsLinkOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_MANAGER_SETTINGS_LINK]


class TournamentManagerSettingsUpdateOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE]
    tournament_id: UUID | None = None
    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=300)
    slug: str = Field(min_length=1, max_length=100)
    type_key: str = Field(min_length=1, max_length=64)
    game_ruleset_key: str = Field(min_length=1, max_length=64)
    visibility: Literal["public", "private"]
    language: str = Field(min_length=1, max_length=35)
    payment_type: Literal["free", "one-time", "per-stage"]
    pricing_plans: list[dict[str, JsonValue]] = Field(default_factory=list)
    registration_open: bool
    registration_open_override: bool | None = Field(default=None, strict=True)
    ignore_late_registrations: bool = True
    registration_starts_at: datetime | None = None
    registration_ends_at: datetime | None = None
    starts_at: datetime | None = None
    planned_ends_at: datetime | None = None
    description: str = Field(default="", max_length=2000)
    author_names: tuple[str, ...] = ()
    author_ids: tuple[UUID, ...] = ()
    default_parameters: dict[str, JsonValue]
    player_mutable_parameters: set[str]
    policies: dict[str, JsonValue]


class TournamentAuthorCreateOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_AUTHOR_CREATE]
    tournament_id: UUID | None = None
    first_name: str = Field(min_length=1, max_length=100)
    second_name: str | None = Field(default=None, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    telegram_link: str | None = Field(default=None, max_length=200)
    return_author: bool = False


class TournamentFinalizeOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_FINALIZE]
    tournament_id: UUID | None = None
    expected_version: int = Field(ge=1)


class TournamentManagerManagementOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_MANAGER_MANAGEMENT]
    tournament_id: UUID | None = None


class TournamentManagerManagementLinkOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_MANAGER_MANAGEMENT_LINK]


class ClassicConfigurationValues(ContractModel):
    stage_type: Literal["none", "groups", "quiz", "playoff"]
    scheme_key: str | None = None
    place_points: list[str | float] = Field(
        default_factory=lambda: ["4", "3", "2", "1"], min_length=1, max_length=12
    )
    score_multiplier: str | float = "0.02"


class ClassicSeedingValues(ContractModel):
    mode: Literal["automatic", "random", "manual"] = "automatic"
    seeds: list[list[UUID | None]] | None = None


class ClassicRoundValues(ContractModel):
    round_id: UUID
    assignment_id: UUID | None = None
    discoverable: bool | None = Field(default=None, strict=True)
    playable: bool | None = Field(default=None, strict=True)
    start_deadline: datetime | None = None


class TournamentClassicUpdateOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_CLASSIC_UPDATE]
    tournament_id: UUID
    expected_version: int = Field(ge=1)
    command: Literal["configure", "seed", "round", "start"]
    kind: Literal["first", "playoff"]
    values: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_values(self):
        schema = {
            "configure": ClassicConfigurationValues,
            "seed": ClassicSeedingValues,
            "round": ClassicRoundValues,
            "start": ContractModel,
        }[self.command]
        parsed = schema.model_validate(self.values)
        object.__setattr__(
            self, "values", parsed.model_dump(mode="json", exclude_unset=self.command == "round")
        )
        return self


class TournamentRegistrationOverrideOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_REGISTRATION_OVERRIDE]
    tournament_id: UUID | None = None
    expected_version: int = Field(ge=1)
    registration_open: bool


class TournamentRegistrationDecideOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_REGISTRATION_DECIDE]
    tournament_id: UUID | None = None
    player_id: UUID
    decision: Literal["approve", "reject"]


class TournamentPacketAccessUpdateOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE]
    tournament_id: UUID | None = None
    assignment_id: UUID
    player_id: UUID | None = None
    right: Literal["playable", "discoverable", "readable"]
    enabled: bool


class TournamentCompleteOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_COMPLETE]
    tournament_id: UUID | None = None
    expected_version: int = Field(ge=1)


class TournamentStartOperation(ContractModel):
    action: Literal[ActionCode.TOURNAMENT_START]
    tournament_id: UUID | None = None
    expected_version: int = Field(ge=1)


class LibraryListOperation(ContractModel):
    action: Literal[ActionCode.LIBRARY_LIST]


class LibraryAccessOperation(ContractModel):
    action: Literal[ActionCode.LIBRARY_VIEW, ActionCode.LIBRARY_DOWNLOAD]
    version_id: UUID
    confirm: bool = Field(default=False, strict=True)


class PlayerListOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_LIST]
    ruleset_key: str | None = Field(default=None, min_length=1, max_length=64)
    search: str = Field(default="", max_length=200)
    order: Literal["name_asc", "name_desc"] = "name_asc"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=20, ge=1, le=100)


class PlayerProfileOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_PROFILE]
    player_id: UUID
    ruleset_key: str | None = Field(default=None, min_length=1, max_length=64)


class PlayerGameResultsOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_GAME_RESULTS]
    player_id: UUID
    game_id: UUID


class PlayerResolveOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_RESOLVE]
    reference: str = Field(min_length=2, max_length=66)


class PacketManagementGetOperation(ContractModel):
    action: Literal[ActionCode.PACKET_MANAGEMENT_GET]
    tournament_id: UUID
    assignment_id: UUID


class PacketManagementUpdateOperation(ContractModel):
    action: Literal[ActionCode.PACKET_MANAGEMENT_UPDATE]
    tournament_id: UUID
    assignment_id: UUID
    expected_version: int = Field(ge=1)
    content: dict[str, JsonValue]
    changes: dict[str, Literal["correction", "substitution"]]
    field_author_ids: dict[str, UUID | None]


class PacketManagementActionOperation(ContractModel):
    action: Literal[ActionCode.PACKET_MANAGEMENT_DELETE, ActionCode.PACKET_MANAGEMENT_RELEASE]
    tournament_id: UUID
    assignment_id: UUID
    expected_version: int = Field(ge=1)


class PacketUploadEligibilityOperation(ContractModel):
    action: Literal[ActionCode.PACKET_UPLOAD_ELIGIBILITY]
    tournament_id: UUID | None = None


class PacketUploadOperation(ContractModel):
    action: Literal[ActionCode.PACKET_UPLOAD]
    tournament_id: UUID | None = None
    source_filename: str = Field(min_length=1, max_length=500)
    source_base64: str = Field(min_length=1, max_length=5_600_000, repr=False)


class PacketDraftGetOperation(ContractModel):
    action: Literal[ActionCode.PACKET_DRAFT_GET]
    draft_id: UUID


class PacketDraftUpdateOperation(ContractModel):
    action: Literal[ActionCode.PACKET_DRAFT_UPDATE]
    draft_id: UUID
    expected_version: int = Field(ge=1)
    content: dict[str, JsonValue]
    author_bindings: dict[str, UUID] | None = None
    lead_author_id: UUID | None = None


class PacketDraftAuthorCreateOperation(ContractModel):
    action: Literal[ActionCode.PACKET_DRAFT_AUTHOR_CREATE]
    draft_id: UUID
    first_name: str = Field(min_length=1, max_length=100)
    second_name: str | None = Field(default=None, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    telegram_link: str | None = Field(default=None, max_length=200)


class PacketDraftTelegramBindOperation(ContractModel):
    action: Literal[ActionCode.PACKET_DRAFT_TELEGRAM_BIND]
    draft_id: UUID
    chat_id: int
    message_id: int = Field(gt=0)
    locale: str = Field(min_length=2, max_length=10)


class PacketDraftDecisionOperation(ContractModel):
    action: Literal[ActionCode.PACKET_DRAFT_PUBLISH, ActionCode.PACKET_DRAFT_REJECT]
    draft_id: UUID


class NotificationsListOperation(ContractModel):
    action: Literal[ActionCode.NOTIFICATIONS_LIST]
    audience: Literal["player", "manager", "admin"] = "player"
    read_state: Literal["all", "seen", "unseen"] = "all"
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class NotificationReadOperation(ContractModel):
    action: Literal[ActionCode.NOTIFICATIONS_READ]
    notification_id: UUID


class NotificationsReadAllOperation(ContractModel):
    action: Literal[ActionCode.NOTIFICATIONS_READ_ALL]
    audience: Literal["player", "manager", "admin"]


class NotificationAlertsClaimOperation(ContractModel):
    action: Literal[ActionCode.NOTIFICATION_ALERTS_CLAIM]
    audience: Literal["player", "manager", "admin"]


class NavigationContextSetOperation(ContractModel):
    action: Literal[ActionCode.NAVIGATION_CONTEXT_SET]
    context: Literal["tournament", "lobby", "lobby_other"]
    expected_version: int = Field(ge=0)


class LobbyJoinOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_JOIN]
    invitation_code: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    role: Literal["player", "observer"] = "player"
    confirm_fresh: bool = False


class LobbyInviteOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_INVITE]
    lobby_id: UUID
    username: str = Field(pattern=r"^@[A-Za-z0-9_]{5,32}$")
    expected_version: int = Field(ge=1)


class LobbyCreateOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_CREATE]
    tournament_id: UUID | None = None
    max_players: int = Field(default=4, ge=1, le=12)


class LobbyLinkOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_LINK]
    lobby_id: UUID


class LobbyInfoOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_INFO]
    lobby_id: UUID


class LobbyEventsOperation(ContractModel):
    action: Literal[ActionCode.LOBBY_EVENTS]
    lobby_id: UUID
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class LobbyVersionedOperation(ContractModel):
    lobby_id: UUID
    expected_version: int = Field(ge=1)


class LobbySettingsUpdateOperation(LobbyVersionedOperation):
    action: Literal[ActionCode.LOBBY_SETTINGS_UPDATE]
    changes: dict[str, JsonValue] = Field(min_length=1)


class LobbyReadyUpdateOperation(LobbyVersionedOperation):
    action: Literal[ActionCode.LOBBY_READY_UPDATE]
    ready: bool


class LobbyRoleUpdateOperation(LobbyVersionedOperation):
    action: Literal[ActionCode.LOBBY_ROLE_UPDATE]
    role: Literal["player", "observer"]
    confirm_fresh: bool = False


class LobbyPacketOperation(LobbyVersionedOperation):
    action: Literal[ActionCode.LOBBY_PACKET_SELECT, ActionCode.LOBBY_PACKET_REMOVE]
    packet_id: UUID


class LobbyPacketBulkSelectOperation(LobbyVersionedOperation):
    action: Literal[ActionCode.LOBBY_PACKET_SELECT_MANY]
    packet_ids: tuple[UUID, ...] = Field(min_length=1)


class LobbySimpleMutationOperation(LobbyVersionedOperation):
    action: Literal[
        ActionCode.LOBBY_LEAVE,
        ActionCode.LOBBY_CANCEL,
        ActionCode.LOBBY_START,
        ActionCode.LOBBY_SEARCH_START,
        ActionCode.LOBBY_SEARCH_CANCEL,
    ]


class GameAppealTicketsOperation(ContractModel):
    action: Literal[ActionCode.GAME_APPEAL_TICKETS]


class GameAppealDecideOperation(ContractModel):
    action: Literal[ActionCode.GAME_APPEAL_DECIDE]
    appeal_id: UUID
    approve: bool


class ChatMembersOperation(ContractModel):
    action: Literal[ActionCode.CHAT_MEMBERS]
    scope: Literal["lobby", "game"]
    scope_id: UUID


class ChatSendOperation(ContractModel):
    action: Literal[ActionCode.CHAT_SEND]
    scope: Literal["lobby", "game"]
    scope_id: UUID
    message_id: int = Field(gt=0)
    text: str | None = Field(default=None, min_length=1, max_length=4096)
    target: str | None = Field(default=None, min_length=1, max_length=200)
    caption: str | None = Field(default=None, max_length=1024)
    media_group_id: str | None = Field(default=None, min_length=1, max_length=128)


class GameViewOperation(ContractModel):
    action: Literal[ActionCode.GAME_VIEW]
    game_id: UUID | None = None
    reconnect: bool = False


class GameActOperation(ContractModel):
    action: Literal[ActionCode.GAME_ACT]
    game_id: UUID
    command: Literal[
        "join",
        "buzz",
        "answer",
        "pause",
        "resume",
        "appeal",
        "vote",
        "escalate",
        "commentary",
        "abandon",
        "observe_leave",
        "quit",
        "reputation",
        "report",
    ]
    round_id: UUID | None = None
    appeal_id: UUID | None = None
    target_id: UUID | None = None
    text: str | None = Field(default=None, min_length=1, max_length=2000, repr=False)
    approve: bool | None = None
    report_kind: Literal["cheating", "toxicity"] | None = None


class GameObserveOperation(ContractModel):
    action: Literal[ActionCode.GAME_OBSERVE]
    game_id: UUID
    confirm_fresh: bool = False


class OngoingListOperation(ContractModel):
    action: Literal[ActionCode.ONGOING_LIST]


class ReputationVoteOperation(ContractModel):
    action: Literal[ActionCode.REPUTATION_VOTE]
    game_id: UUID
    expected_version: int = Field(ge=1)
    target_telegram_user_id: int = Field(gt=0)
    value: Literal[-1, 1]


class PlayerReportOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_REPORT]
    game_id: UUID
    expected_version: int = Field(ge=1)
    target_telegram_user_id: int = Field(gt=0)
    kind: Literal["cheating", "toxicity"]
    details: str | None = Field(default=None, max_length=2000)


class PlayerBanOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_BAN]
    target: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)


class PlayerUnbanOperation(ContractModel):
    action: Literal[ActionCode.PLAYER_UNBAN]
    target: str = Field(min_length=1, max_length=200)


class BugReportCreateOperation(ContractModel):
    action: Literal[ActionCode.BUG_REPORT_CREATE]
    commentary: str = Field(min_length=1, max_length=4000)


class AdminManagementListOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_MANAGEMENT_LIST]
    section: Literal["tournaments", "authors", "players", "packets", "link_requests"] = (
        "tournaments"
    )


class AdminTournamentModerateOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_TOURNAMENT_MODERATE]
    tournament_id: UUID
    command: Literal["halt", "resume", "abolish"]
    expected_version: int = Field(ge=1)
    confirm: bool = Field(default=False, strict=True)


class AdminAuthorLinkOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_AUTHOR_LINK]
    author_id: UUID
    target: str = Field(min_length=1, max_length=200)


class AdminAuthorMergeOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_AUTHOR_MERGE]
    author_id: UUID
    merge_author_id: UUID
    confirm: bool = Field(default=False, strict=True)


class AdminPacketAccessOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_PACKET_ACCESS]
    version_id: UUID
    command: Literal["view", "download"]
    confirm: bool = Field(default=False, strict=True)


class AdminSuspicionLedgerOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_SUSPICION_LEDGER]
    limit: int = Field(default=50, ge=1, le=100)


class AdminSuspicionInspectOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_SUSPICION_INSPECT]
    player_id: UUID
    limit: int = Field(default=100, ge=1, le=100)


class AdminSuspicionClearOperation(ContractModel):
    action: Literal[ActionCode.ADMIN_SUSPICION_CLEAR]
    player_id: UUID
    note: str = Field(min_length=1, max_length=2000)


GatewayOperation = Annotated[
    CapabilitiesOperation
    | AdminAuthenticateOperation
    | AccountLookupOperation
    | RegistrationStartOperation
    | RegistrationStepSaveOperation
    | RegistrationCompleteOperation
    | TelegramUsernameRefreshOperation
    | SettingsListOperation
    | SettingsUpdateOperation
    | NavigationSnapshotOperation
    | NavigationModeSetOperation
    | NavigationTournamentSetOperation
    | AuthorsSearchOperation
    | AuthorLinkCreateOperation
    | AuthorLinkCancelOperation
    | AuthorLinkMineOperation
    | AuthorLinkAdminPendingOperation
    | AuthorLinkAdminDecideOperation
    | TokenRequestCreateOperation
    | TokenRequestQueueOperation
    | TokenRequestAdminDecideOperation
    | TokenInventoryOperation
    | TokenClaimOperation
    | TournamentCreateOperation
    | TournamentListOperation
    | TournamentInfoOperation
    | TournamentProfileOperation
    | TournamentRegisterOperation
    | TournamentRegistrationLinkOperation
    | TournamentRegistrationInvitationOperation
    | TournamentManagerSettingsOperation
    | TournamentManagerSettingsLinkOperation
    | TournamentManagerSettingsUpdateOperation
    | TournamentAuthorCreateOperation
    | TournamentFinalizeOperation
    | TournamentManagerManagementOperation
    | TournamentManagerManagementLinkOperation
    | TournamentRegistrationOverrideOperation
    | TournamentClassicUpdateOperation
    | TournamentRegistrationDecideOperation
    | TournamentPacketAccessUpdateOperation
    | PacketManagementGetOperation
    | LibraryListOperation
    | LibraryAccessOperation
    | PlayerListOperation
    | PlayerProfileOperation
    | PlayerGameResultsOperation
    | PlayerResolveOperation
    | PacketManagementUpdateOperation
    | PacketManagementActionOperation
    | TournamentCompleteOperation
    | TournamentStartOperation
    | PacketUploadEligibilityOperation
    | PacketUploadOperation
    | PacketDraftGetOperation
    | PacketDraftUpdateOperation
    | PacketDraftAuthorCreateOperation
    | PacketDraftTelegramBindOperation
    | PacketDraftDecisionOperation
    | NotificationsListOperation
    | NotificationReadOperation
    | NotificationsReadAllOperation
    | NotificationAlertsClaimOperation
    | NavigationContextSetOperation
    | LobbyJoinOperation
    | LobbyInviteOperation
    | LobbyCreateOperation
    | LobbyLinkOperation
    | LobbyInfoOperation
    | LobbyEventsOperation
    | LobbySettingsUpdateOperation
    | LobbyReadyUpdateOperation
    | LobbyRoleUpdateOperation
    | LobbyPacketOperation
    | LobbyPacketBulkSelectOperation
    | LobbySimpleMutationOperation
    | GameViewOperation
    | GameActOperation
    | GameObserveOperation
    | OngoingListOperation
    | ChatMembersOperation
    | ChatSendOperation
    | GameAppealTicketsOperation
    | GameAppealDecideOperation
    | ReputationVoteOperation
    | PlayerReportOperation
    | PlayerBanOperation
    | PlayerUnbanOperation
    | BugReportCreateOperation
    | AdminSuspicionLedgerOperation
    | AdminSuspicionInspectOperation
    | AdminSuspicionClearOperation
    | AdminManagementListOperation
    | AdminTournamentModerateOperation
    | AdminAuthorLinkOperation
    | AdminAuthorMergeOperation
    | AdminPacketAccessOperation,
    Field(discriminator="action"),
]


class GatewayRequest(ContractModel):
    metadata: RequestMetadata
    operation: GatewayOperation


class GatewayError(ContractModel):
    code: ErrorCode
    message_key: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class GatewayResponse(ContractModel):
    contract_version: Literal["1.0"] = "1.0"
    action: ActionCode
    correlation_id: UUID
    ok: bool
    data: JsonValue = None
    error: GatewayError | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "GatewayResponse":
        if self.ok and self.error is not None:
            raise ValueError("A successful gateway response cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("A failed gateway response requires an error")
        return self


class CapabilityAction(ContractModel):
    action: ActionCode
    authentication_required: bool
    mutation: bool
    idempotency_required: bool
    stale_write_field: str | None = None
    cursor_paginated: bool = False
    sensitive_response: bool = False


class CapabilityPayload(ContractModel):
    contract_version: Literal["1.0"] = "1.0"
    minimum_client_version: str
    actions: tuple[CapabilityAction, ...]
    features: tuple[str, ...]
