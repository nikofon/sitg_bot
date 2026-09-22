from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FrontendState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountState(FrontendState):
    player_id: UUID
    telegram_user_id: int | None
    public_nickname: str | None
    preferred_locale: str
    registration_status: str
    registration_completed_at: str | None
    profile_version: int


class RegistrationState(FrontendState):
    account: AccountState
    next_step: str | None
    completed_steps: tuple[str, ...]


class SettingChoiceState(FrontendState):
    value: str | bool
    labels: dict[str, str]


class SettingState(FrontendState):
    key: str
    value_type: str
    labels: dict[str, str]
    value: str | bool
    localized_value: dict[str, str]
    choices: tuple[SettingChoiceState, ...] = ()


class SettingsState(FrontendState):
    profile_version: int
    updated_at: str
    settings: tuple[SettingState, ...]


class TokenDeliveryState(FrontendState):
    status: str
    delivered_at: str | None
    failed_at: str | None
    failure_reason: str | None


class TokenRequestState(FrontendState):
    request_id: UUID
    requester_id: UUID
    requester_nickname: str
    requester_real_name: str | None = None
    requester_telegram_username: str | None = None
    requester_telegram_user_id: int | None = None
    tournament_name: str | None = None
    status: str
    justification: str | None
    decision_note: str | None
    decided_by_id: UUID | None
    created_at: str
    decided_at: str | None
    token_id: UUID | None
    token_fingerprint: str | None
    token_expires_at: str | None
    delivery: TokenDeliveryState | None


class TokenRequestPageState(FrontendState):
    items: tuple[TokenRequestState, ...]
    next_cursor: str | None


class TokenInventoryItemState(FrontendState):
    source: str
    request_id: UUID | None
    request_status: str | None
    requested_at: str | None
    tournament_name: str | None = None
    decision_note: str | None = None
    token_id: UUID | None
    fingerprint: str | None
    token_status: str | None
    issued_at: str | None
    expires_at: str | None
    used_at: str | None
    tournament_id: UUID | None
    revoked_at: str | None
    delivery: TokenDeliveryState | None


class TokenInventoryState(FrontendState):
    items: tuple[TokenInventoryItemState, ...]
    next_cursor: str | None


class DeliveredCreationTokenState(FrontendState):
    token_id: UUID
    token: str
    fingerprint: str
    expires_at: str


class TournamentCreatedState(FrontendState):
    id: UUID
    name: str
    slug: str


class TournamentSettingsLinkState(FrontendState):
    value: str
    expires_at: str

    @property
    def telegram_payload(self) -> str:
        return f"lr_{self.value}"


class PacketDraftState(FrontendState):
    draft_id: UUID
    version: int
    status: str
    source_filename: str
    ruleset_key: str
    ruleset_version: int
    packet_name: str
    theme_count: int | None
    question_count: int | None
    detected_authors: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    can_publish: bool
    can_reject: bool
    launch_reference: str | None = None
    launch_expires_at: str | None = None

    @property
    def value(self) -> str:
        return self.launch_reference or ""

    @property
    def telegram_payload(self) -> str:
        return f"lr_{self.value}"


class LobbyCreatedState(FrontendState):
    lobby_id: UUID
    version: int
    tournament_id: UUID
    status: str
    launch_reference: str
    launch_expires_at: str

    @property
    def value(self) -> str:
        return self.launch_reference

    @property
    def telegram_payload(self) -> str:
        return f"lr_{self.launch_reference}"


class NotificationState(FrontendState):
    notification_id: UUID
    kind: str
    payload: dict[str, object]
    created_at: str
    read_at: str | None


class NotificationPageState(FrontendState):
    items: tuple[NotificationState, ...]
    next_cursor: str | None


class NotificationReadResultState(FrontendState):
    read_count: int


class NotificationAlertState(FrontendState):
    audiences: tuple[str, ...]


class NavigationTournamentActionState(FrontendState):
    key: str
    descriptor_version: int


class NavigationTournamentState(FrontendState):
    id: UUID
    name: str
    slug: str
    status: str
    capability_actions: tuple[NavigationTournamentActionState, ...] = ()


class NavigationLobbyState(FrontendState):
    id: UUID
    tournament_id: UUID
    status: str
    version: int


class NavigationGameState(FrontendState):
    id: UUID
    tournament_id: UUID
    status: str
    phase: str
    version: int
    joined: bool
    active: bool
    can_reconnect: bool


class NavigationChatState(FrontendState):
    id: UUID
    tournament_id: UUID
    tournament_name: str
    round_number: int
    match_number: int
    multiple_matches: bool


class NavigationState(FrontendState):
    account: AccountState
    available_modes: tuple[str, ...]
    active_mode: str
    context: str
    navigation_version: int
    selected_player_tournament: NavigationTournamentState | None
    selected_manager_tournament: NavigationTournamentState | None
    active_lobby: NavigationLobbyState | None
    active_game: NavigationGameState | None
    allowed_actions: tuple[str, ...]
    ban_reason: str | None = None
    active_chat: NavigationChatState | None = None
