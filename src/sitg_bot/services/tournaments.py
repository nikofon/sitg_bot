from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.appeals import AppealPolicy
from sitg_bot.domain.game_rulesets import (
    DEFAULT_RULESETS,
    GameRuleset,
    GameRulesetRegistry,
    RulesetParameters,
)
from sitg_bot.domain.game_settings import MAX_THEME_COUNT
from sitg_bot.domain.packet import normalize_language_tag
from sitg_bot.services.author_exposure import burn_author_content, tournament_manager_ids
from sitg_bot.services.classic import ClassicService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.subscriptions import SubscriptionService, subscription_snapshot
from sitg_bot.storage.authorship import packet_author_ids
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    GameRulesetVersionRecord,
    LogicalPacketRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerExposureClaimRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
    PregameLobbyEventRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    TournamentAuthorRecord,
    TournamentCreationTokenDeliveryRecord,
    TournamentCreationTokenRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TournamentPolicyVersionRecord,
    TournamentPricingPlanPriceRecord,
    TournamentPricingPlanRecord,
    TournamentRecord,
    TournamentRegistrationAttemptRecord,
    TournamentRegistrationRequirementRecord,
    TournamentTypeVersionRecord,
)

PACKET_RIGHTS = frozenset({"discoverable", "playable", "content_visible", "editable"})
LIBRARY_VIEWING_RULES = frozenset({"never", "after-play", "anytime"})
PAYMENT_TYPES = frozenset({"free", "one-time", "per-stage"})
_UNSET = object()
HYBRID_MATCHMAKING_POLICY = "hybrid_matchmaking_enabled"
AUTO_APPROVE_REGISTRATIONS_POLICY = "auto_approve_registrations"
MEMBER_UPLOADS_POLICY = "member_uploads"
PACKET_NOTIFICATIONS_POLICY = "packet_notifications_enabled"
RULESET_RATING_WEIGHT_POLICY = "ruleset_rating_weight"
MAXIMUM_PARTICIPANTS_POLICY = "maximum_participants"
OBSERVING_POLICY = "observing"
OBSERVING_POLICIES = frozenset({"unlimited", "burnt-only", "forbidden"})
PACKETS_PER_LOBBY_POLICY = "packets_per_lobby"
PACKETS_PER_LOBBY_VALUES = frozenset({"one", "any"})
PACKET_ACCESS_DEFAULT_POLICIES = {
    "packets_discoverable_by_default": True,
    "packets_playable_by_default": False,
    "packets_readable_by_default": False,
    "packets_released_by_default": False,
}
REGISTRATION_REQUIREMENT_KINDS = frozenset(
    {"has-played-tournament", "has-not-played-tournament", "has-not-seen-packet"}
)


def tournament_parameters(
    type_key: str, ruleset_key: str, parameters: dict[str, object] | None,
    mutable: set[str] | frozenset[str] | list[str],
) -> tuple[dict[str, object], frozenset[str]]:
    """Classic SI uses the maximum; each lobby resolves it to its full packet size.

    A ``max`` theme count is kept as the raw sentinel here; every caller that
    builds ruleset parameters substitutes the ruleset maximum for it.
    """
    parameters = dict(parameters or {})
    mutable = frozenset(mutable)
    if type_key == "classic" and ruleset_key == "si":
        parameters["theme_count"] = 128
        mutable -= {"theme_count"}
    return parameters, mutable


def hybrid_matchmaking_supported(type_rules: dict[str, object]) -> bool:
    return type_rules.get("supports_hybrid_matchmaking") is True


def normalize_tournament_policies(
    type_rules: dict[str, object], policies: dict[str, object] | None
) -> dict[str, object]:
    normalized = dict(policies or {})
    library_viewing_rule = normalized.setdefault("library_viewing_rule_default", "after-play")
    if (
        not isinstance(library_viewing_rule, str)
        or library_viewing_rule not in LIBRARY_VIEWING_RULES
    ):
        raise ValueError("Unknown default library viewing rule")
    for name, default in PACKET_ACCESS_DEFAULT_POLICIES.items():
        if not isinstance(normalized.setdefault(name, default), bool):
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(normalized.setdefault(AUTO_APPROVE_REGISTRATIONS_POLICY, False), bool):
        raise ValueError(f"{AUTO_APPROVE_REGISTRATIONS_POLICY} must be a boolean")
    if not isinstance(normalized.setdefault(MEMBER_UPLOADS_POLICY, False), bool):
        raise ValueError(f"{MEMBER_UPLOADS_POLICY} must be a boolean")
    if not isinstance(normalized.setdefault(PACKET_NOTIFICATIONS_POLICY, True), bool):
        raise ValueError(f"{PACKET_NOTIFICATIONS_POLICY} must be a boolean")
    enabled = normalized.setdefault(HYBRID_MATCHMAKING_POLICY, False)
    if not isinstance(enabled, bool):
        raise ValueError(f"{HYBRID_MATCHMAKING_POLICY} must be a boolean")
    if enabled and not hybrid_matchmaking_supported(type_rules):
        raise ValueError("Tournament type does not support hybrid matchmaking")
    weight = normalized.setdefault(RULESET_RATING_WEIGHT_POLICY, 1)
    if isinstance(weight, bool) or not isinstance(weight, int | float):
        raise ValueError(f"{RULESET_RATING_WEIGHT_POLICY} must be a number")
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError(f"{RULESET_RATING_WEIGHT_POLICY} must be finite and positive")
    decimal_weight = Decimal(str(weight))
    if decimal_weight.as_tuple().exponent < -4:
        raise ValueError(f"{RULESET_RATING_WEIGHT_POLICY} supports at most four decimals")
    maximum_participants = normalized.get(MAXIMUM_PARTICIPANTS_POLICY)
    if maximum_participants is not None and (
        isinstance(maximum_participants, bool)
        or not isinstance(maximum_participants, int)
        or maximum_participants < 1
    ):
        raise ValueError(f"{MAXIMUM_PARTICIPANTS_POLICY} must be a positive integer or null")
    observing = normalized.setdefault(OBSERVING_POLICY, "forbidden")
    if not isinstance(observing, str) or observing not in OBSERVING_POLICIES:
        raise ValueError(f"{OBSERVING_POLICY} must be unlimited, burnt-only, or forbidden")
    packets_per_lobby = normalized.setdefault(PACKETS_PER_LOBBY_POLICY, "one")
    if (
        not isinstance(packets_per_lobby, str)
        or packets_per_lobby not in PACKETS_PER_LOBBY_VALUES
    ):
        raise ValueError(f"{PACKETS_PER_LOBBY_POLICY} must be one or any")
    return normalized


def ruleset_default_settings(
    ruleset: GameRuleset, default_parameters: dict[str, object]
) -> tuple[RulesetParameters, dict[str, object]]:
    """Validate raw defaults and keep the maximum-themes sentinel in the raw dict."""
    raw = dict(default_parameters)
    maximum_themes = raw.get("theme_count") == MAX_THEME_COUNT
    if maximum_themes:
        del raw["theme_count"]
    settings = ruleset.parameters(raw)
    stored = settings.to_dict()
    if maximum_themes:
        stored["theme_count"] = MAX_THEME_COUNT
    return settings, stored


@dataclass(frozen=True, slots=True)
class TournamentContext:
    tournament_id: UUID
    type_version_id: UUID
    type_key: str
    game_ruleset_version_id: UUID
    ruleset_key: str
    ruleset_version: int
    policy_version_id: UUID
    policy_version: int
    settings: RulesetParameters
    mutable_parameters: frozenset[str]
    policies: dict[str, object]
    type_rules: dict[str, object]
    assembly_open: bool

    @property
    def type_supports_hybrid_matchmaking(self) -> bool:
        return hybrid_matchmaking_supported(self.type_rules)

    @property
    def hybrid_matchmaking_enabled(self) -> bool:
        return (
            self.type_supports_hybrid_matchmaking
            and self.policies.get(HYBRID_MATCHMAKING_POLICY) is True
        )

    @property
    def observing_policy(self) -> str:
        return str(self.policies.get(OBSERVING_POLICY, "forbidden"))


@dataclass(frozen=True, slots=True)
class TournamentSnapshot:
    id: UUID
    name: str
    slug: str
    status: str
    type_key: str
    type_version: int
    ruleset_key: str
    ruleset_version: int
    policy_version: int
    visibility: str
    starts_at: datetime | None
    planned_ends_at: datetime | None
    actual_ends_at: datetime | None
    language: str
    payment_type: str
    pricing_plans: tuple[PricingPlanSnapshot, ...]
    registration_open: bool
    registration_starts_at: datetime | None
    registration_ends_at: datetime | None
    finalized_at: datetime | None
    settings_version: int
    actual_starts_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TournamentListItem:
    id: UUID
    name: str
    slug: str
    status: str
    visibility: str
    starts_at: datetime | None
    planned_ends_at: datetime | None
    actual_ends_at: datetime | None
    language: str
    payment_type: str
    pricing_plans: tuple[PricingPlanSnapshot, ...]
    registration_open: bool
    registration_starts_at: datetime | None
    registration_ends_at: datetime | None
    authors: tuple[str, ...]
    type_key: str
    type_version: int
    ruleset_key: str
    ruleset_version: int
    joinable: bool
    membership_status: str | None = None
    phase: str = "ongoing"
    managed: bool = False
    policy_version: int = 1
    available_actions: tuple[str, ...] = ()
    finalized_at: datetime | None = None
    settings_version: int = 1
    actual_starts_at: datetime | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class TournamentListing:
    ongoing: tuple[TournamentListItem, ...]
    future: tuple[TournamentListItem, ...]
    past: tuple[TournamentListItem, ...]


@dataclass(frozen=True, slots=True)
class TournamentListPage:
    items: tuple[TournamentListItem, ...]
    next_cursor: str | None
    total: int
    navigation_version: int


@dataclass(frozen=True, slots=True)
class TournamentDetails:
    tournament: TournamentListItem
    registration_requirements: tuple[RegistrationRequirementSnapshot, ...]
    policies: dict[str, object]
    default_parameters: dict[str, object]
    player_mutable_parameters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerSettingDescriptor:
    name: str
    value_type: str
    description_key: str
    value: object
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagerAuthorDescriptor:
    id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class TournamentManagerSettings:
    tournament: TournamentListItem
    settings_version: int
    finalized_at: datetime | None
    registration_enabled: bool
    ignore_late_registrations: bool
    available_actions: tuple[str, ...]
    type_options: tuple[str, ...]
    ruleset_options: tuple[str, ...]
    policies: dict[str, object]
    default_parameters: dict[str, object]
    player_mutable_parameters: tuple[str, ...]
    author_names: tuple[str, ...]
    authors: tuple[ManagerAuthorDescriptor, ...]
    setting_descriptors: tuple[ManagerSettingDescriptor, ...]
    policy_descriptors: tuple[ManagerSettingDescriptor, ...]
    registration_requirements: tuple[RegistrationRequirementSnapshot, ...]
    packet_assignment_count: int
    membership_count: int
    manager_count: int
    organizer_contacts: str = ""
    channel: str = ""
    classic: dict | None = None


@dataclass(frozen=True, slots=True)
class ManagementRegistration:
    player_id: UUID
    display_name: str
    real_name: str | None
    status: str
    registered_at: datetime
    available_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagementPacketAccess:
    player_id: UUID
    display_name: str
    playable: bool
    discoverable: bool
    readable: bool


@dataclass(frozen=True, slots=True)
class ManagementPacket:
    assignment_id: UUID
    packet_id: UUID
    name: str
    version: int | None
    player_access: tuple[ManagementPacketAccess, ...]
    year: int | None = None
    published_at: datetime | None = None
    lead_author: str | None = None
    authors: tuple[str, ...] = ()
    released: bool = False
    packet_version_id: UUID | None = None
    default_access: dict[str, bool] | None = None
    library_viewing_rule: str = "after-play"


@dataclass(frozen=True, slots=True)
class TournamentManagement:
    tournament: TournamentListItem
    sections: tuple[str, ...]
    settings_version: int
    finalized_at: datetime | None
    registration_scheduled_open: bool
    registration_open: bool
    registration_open_override: bool | None
    registration_count: int
    approved_count: int
    participant_count: int
    packet_count: int
    registrations: tuple[ManagementRegistration, ...]
    packets: tuple[ManagementPacket, ...]
    available_actions: tuple[str, ...]
    classic: dict | None = None
    subscriptions: dict | None = None


@dataclass(frozen=True, slots=True)
class RegistrationRequirementSnapshot:
    id: UUID
    kind: str
    target_id: UUID
    target_name: str
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class PricingPlanPriceSnapshot:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class PricingPlanSnapshot:
    id: UUID
    name: str
    prices: tuple[PricingPlanPriceSnapshot, ...]


@dataclass(frozen=True, slots=True)
class RegistrationDecision:
    accepted: bool
    status: str
    reasons: tuple[str, ...] = ()


class TournamentService:
    def __init__(
        self,
        database: Database,
        *,
        rulesets: GameRulesetRegistry = DEFAULT_RULESETS,
        token_lifetime: timedelta = timedelta(hours=24),
    ) -> None:
        if token_lifetime <= timedelta(0):
            raise ValueError("Tournament creation token lifetime must be positive")
        self.database = database
        self.rulesets = rulesets
        self.token_lifetime = token_lifetime

    @staticmethod
    async def player_tournament_map(
        session: AsyncSession, player_id: UUID
    ) -> tuple[dict[UUID, str], frozenset[UUID]]:
        """Names of tournaments the player participates in or manages, plus managed IDs."""
        names = {
            tournament_id: name
            for tournament_id, name in (
                await session.execute(
                    select(TournamentRecord.id, TournamentRecord.name)
                    .join(
                        TournamentMembershipRecord,
                        TournamentMembershipRecord.tournament_id
                        == TournamentRecord.id,
                    )
                    .where(
                        TournamentMembershipRecord.player_id == player_id,
                        TournamentMembershipRecord.status == "active",
                    )
                    .order_by(TournamentRecord.name)
                )
            ).all()
        }
        managed_rows = (
            await session.execute(
                select(TournamentRecord.id, TournamentRecord.name)
                .join(
                    TournamentManagerRecord,
                    TournamentManagerRecord.tournament_id == TournamentRecord.id,
                )
                .where(
                    TournamentManagerRecord.player_id == player_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
                .order_by(TournamentRecord.name)
            )
        ).all()
        for tournament_id, name in managed_rows:
            names.setdefault(tournament_id, name)
        managed_ids = frozenset(row[0] for row in managed_rows)
        return names, managed_ids

    async def grant_administrator(
        self, player_id: UUID, *, granted_by_id: UUID | None = None
    ) -> None:
        async with self.database.transaction() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            administrator_count = await session.scalar(
                select(func.count())
                .select_from(PlatformAdministratorRecord)
                .where(PlatformAdministratorRecord.revoked_at.is_(None))
            )
            if administrator_count and granted_by_id is None:
                raise PermissionError("An active administrator must grant this role")
            if granted_by_id is not None:
                await self._require_administrator(session, granted_by_id)
            record = await session.get(PlatformAdministratorRecord, player_id)
            if record is None:
                session.add(
                    PlatformAdministratorRecord(player_id=player_id, granted_by_id=granted_by_id)
                )
            else:
                record.granted_by_id = granted_by_id
                record.revoked_at = None

    async def issue_creation_token(
        self,
        administrator_id: UUID,
        *,
        intended_creator_id: UUID | None = None,
        lifetime: timedelta | None = None,
    ) -> str:
        ttl = lifetime or self.token_lifetime
        if ttl <= timedelta(0):
            raise ValueError("Tournament creation token lifetime must be positive")
        raw_token = secrets.token_urlsafe(32)
        token_digest = self._token_digest(raw_token)
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            if intended_creator_id is not None:
                intended_creator = await session.get(PlayerRecord, intended_creator_id)
                if intended_creator is None or intended_creator.status != "active":
                    raise LookupError("Active intended tournament creator not found")
            session.add(
                TournamentCreationTokenRecord(
                    token_digest=token_digest,
                    fingerprint=token_digest[:12],
                    issued_by_id=administrator_id,
                    intended_creator_id=intended_creator_id,
                    expires_at=datetime.now(UTC) + ttl,
                )
            )
        return raw_token

    async def revoke_creation_token(self, raw_token: str, *, administrator_id: UUID) -> None:
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            token = await session.scalar(
                select(TournamentCreationTokenRecord)
                .where(TournamentCreationTokenRecord.token_digest == self._token_digest(raw_token))
                .with_for_update()
            )
            if token is None:
                raise LookupError("Tournament creation token not found")
            if token.used_at is not None:
                raise ValueError("A used tournament creation token cannot be revoked")
            now = datetime.now(UTC)
            token.revoked_at = now
            token.revoked_by_id = administrator_id
            delivery = await session.get(TournamentCreationTokenDeliveryRecord, token.id)
            if delivery is not None and delivery.status == "pending":
                delivery.status = "failed"
                delivery.failed_at = now
                delivery.failure_reason = "revoked"
                delivery.encrypted_token = None

    @staticmethod
    def _normalize_description(description: object) -> str:
        if not isinstance(description, str):
            raise ValueError("Tournament description must be a string")
        normalized = description.strip()
        if len(normalized) > 2000:
            raise ValueError("Tournament description must be at most 2000 characters")
        return normalized

    @staticmethod
    def _normalize_optional_text(value: object, field: str, *, max_length: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Tournament {field} must be a string")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise ValueError(f"Tournament {field} must be at most {max_length} characters")
        return normalized

    async def create_tournament(
        self,
        *,
        raw_token: str | None = None,
        token_id: UUID | None = None,
        creator_id: UUID,
        name: str,
        slug: str,
        type_key: str,
        game_ruleset_key: str = "si",
        default_parameters: dict[str, object] | None = None,
        player_mutable_parameters: set[str] | frozenset[str] = frozenset(),
        policies: dict[str, object] | None = None,
        visibility: str = "private",
        language: str = "und",
        payment_type: str = "free",
        pricing_plans: object = (),
        registration_open: bool = False,
        ignore_late_registrations: bool = True,
        registration_starts_at: datetime | None = None,
        registration_ends_at: datetime | None = None,
        starts_at: datetime | None = None,
        planned_ends_at: datetime | None = None,
        description: str = "",
        author_names: tuple[str, ...] = (),
    ) -> TournamentSnapshot:
        normalized_description = self._normalize_description(description)
        if (raw_token is None) == (token_id is None):
            raise ValueError("Exactly one tournament creation token reference is required")
        normalized_name = name.strip()
        normalized_slug = slug.strip().casefold()
        if not normalized_name:
            raise ValueError("Tournament name cannot be empty")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized_slug):
            raise ValueError("Tournament slug must contain lowercase letters, numbers, and hyphens")
        normalized_visibility = visibility.strip().casefold()
        if normalized_visibility not in {"public", "private"}:
            raise ValueError("Tournament visibility must be public or private")
        normalized_language = normalize_language_tag(language)
        if not isinstance(registration_open, bool):
            raise ValueError("registration_open must be a boolean")
        if not isinstance(ignore_late_registrations, bool):
            raise ValueError("ignore_late_registrations must be a boolean")
        normalized_payment_type, normalized_pricing_plans = self._pricing(
            payment_type, pricing_plans
        )
        self._validate_schedule(
            registration_starts_at,
            registration_ends_at,
            starts_at,
            planned_ends_at,
        )
        AppealPolicy.from_mapping(policies)

        async with self.database.transaction() as session:
            creator = await session.get(PlayerRecord, creator_id)
            if creator is None or creator.status != "active":
                raise LookupError("Active tournament creator not found")
            token_statement = select(TournamentCreationTokenRecord)
            if token_id is not None:
                token_statement = token_statement.where(
                    TournamentCreationTokenRecord.id == token_id
                )
            else:
                assert raw_token is not None
                token_statement = token_statement.where(
                    TournamentCreationTokenRecord.token_digest == self._token_digest(raw_token)
                )
            token = await session.scalar(token_statement.with_for_update())
            now = datetime.now(UTC)
            if token is None:
                raise ValueError("Invalid tournament creation token")
            if token.revoked_at is not None:
                raise ValueError("Tournament creation token was revoked")
            if token.used_at is not None:
                raise ValueError("Tournament creation token was already used")
            if token.expires_at <= now:
                raise ValueError("Tournament creation token has expired")
            if token.intended_creator_id not in (None, creator_id):
                raise PermissionError("Tournament creation token belongs to another creator")

            type_version = await self._latest_type(session, type_key)
            normalized_policies = normalize_tournament_policies(type_version.rules, policies)
            ruleset_version = await self._latest_ruleset(session, game_ruleset_key)
            compatible = type_version.rules.get("compatible_rulesets")
            if compatible and game_ruleset_key not in compatible:
                raise ValueError("Tournament type is incompatible with the selected game ruleset")
            ruleset = self.rulesets.get(ruleset_version.key, ruleset_version.version)
            default_parameters, player_mutable_parameters = tournament_parameters(
                type_key, ruleset_version.key, default_parameters, player_mutable_parameters
            )
            settings, stored_defaults = ruleset_default_settings(ruleset, default_parameters)
            mutable = frozenset(player_mutable_parameters)
            unknown_mutable = mutable - ruleset.parameter_names
            if unknown_mutable:
                raise ValueError(f"Unknown mutable parameter: {sorted(unknown_mutable)[0]}")

            tournament = TournamentRecord(
                name=normalized_name,
                slug=normalized_slug,
                type_version_id=type_version.id,
                game_ruleset_version_id=ruleset_version.id,
                created_by_id=creator_id,
                visibility=normalized_visibility,
                language=normalized_language,
                payment_type=normalized_payment_type,
                registration_open=registration_open,
                ignore_late_registrations=ignore_late_registrations,
                registration_starts_at=registration_starts_at,
                registration_ends_at=registration_ends_at,
                starts_at=starts_at,
                planned_ends_at=planned_ends_at,
                description=normalized_description,
                finalized_at=None,
            )
            session.add(tournament)
            await session.flush()
            await self._replace_pricing_plans(
                session,
                tournament.id,
                normalized_pricing_plans,
                actor_id=creator_id,
            )
            normalized_author_names = dict.fromkeys(
                self._normalize_author_name(name) for name in author_names if name.strip()
            )
            for author_name in normalized_author_names:
                author = await self._create_author(session, author_name)
                session.add(
                    TournamentAuthorRecord(
                        tournament_id=tournament.id,
                        author_id=author.id,
                        added_by_id=creator_id,
                    )
                )
            policy = TournamentPolicyVersionRecord(
                tournament_id=tournament.id,
                version=1,
                default_parameters=stored_defaults,
                player_mutable_parameters=sorted(mutable),
                policies=normalized_policies,
                created_by_id=creator_id,
            )
            session.add_all(
                (
                    policy,
                    TournamentManagerRecord(
                        tournament_id=tournament.id,
                        player_id=creator_id,
                        granted_by_id=creator_id,
                    ),
                )
            )
            await session.flush()
            token.used_at = now
            token.used_by_id = creator_id
            token.tournament_id = tournament.id
            delivery = await session.get(TournamentCreationTokenDeliveryRecord, token.id)
            if delivery is not None and delivery.status == "pending":
                delivery.status = "failed"
                delivery.failed_at = now
                delivery.failure_reason = "consumed_without_plaintext_delivery"
                delivery.encrypted_token = None
            return self._snapshot(
                tournament,
                type_version,
                ruleset_version,
                policy.version,
                await self.pricing_plans(session, tournament.id),
            )

    async def list_public(self, *, now: datetime | None = None) -> TournamentListing:
        reference_time = self._reference_time(now)
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    self._listing_query().where(
                        TournamentRecord.visibility == "public",
                        TournamentRecord.status != "draft",
                        TournamentRecord.finalized_at.is_not(None),
                    )
                )
            ).all()
            return await self._group_listing(session, rows, reference_time)

    async def list_for_player(
        self, player_id: UUID, *, now: datetime | None = None
    ) -> TournamentListing:
        reference_time = self._reference_time(now)
        async with self.database.sessions() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            rows = (
                await session.execute(
                    self._listing_query(include_membership=True).where(
                        TournamentMembershipRecord.player_id == player_id,
                        TournamentRecord.finalized_at.is_not(None),
                    )
                )
            ).all()
            return await self._group_listing(session, rows, reference_time, include_membership=True)

    async def list_visible(
        self,
        player_id: UUID | None,
        *,
        role: str = "player",
        include_managed_public: bool = False,
        phase: str | None = None,
        relationship: str | None = None,
        registration: str | None = None,
        type_key: str | None = None,
        ruleset_key: str | None = None,
        language: str | None = None,
        search: str = "",
        order: str = "starts_asc",
        cursor: str | None = None,
        limit: int = 20,
        now: datetime | None = None,
    ) -> TournamentListPage:
        """Return the caller's tournament catalogue without trusting frontend role claims."""

        normalized = self._normalize_listing_options(
            role=role,
            phase=phase,
            relationship=relationship,
            registration=registration,
            type_key=type_key,
            ruleset_key=ruleset_key,
            language=language,
            search=search,
            order=order,
            limit=limit,
        )
        reference_time = self._reference_time(now)
        async with self.database.sessions() as session:
            rows = await self._visible_listing_rows(
                session, player_id=player_id, role=normalized["role"],
                include_managed_public=include_managed_public,
            )
            rows = self._filter_visible_rows(rows, normalized, reference_time)
            self._sort_visible_rows(rows, normalized["order"])
            signature = self._listing_signature(normalized)
            offset = self._decode_listing_cursor(cursor, signature)
            page_rows = rows[offset : offset + limit]
            items = [
                await self._visible_list_item(
                    session, row, reference_time, normalized["role"] if player_id else "guest"
                )
                for row in page_rows
            ]
            visible = tuple(items)
            next_offset = offset + len(items)
            return TournamentListPage(
                items=visible,
                next_cursor=(
                    self._encode_listing_cursor(next_offset, signature)
                    if next_offset < len(rows)
                    else None
                ),
                total=len(rows),
                navigation_version=(
                    navigation.version
                    if player_id is not None
                    and (navigation := await session.get(PlayerTelegramNavigationRecord, player_id))
                    is not None
                    else 0
                ),
            )

    async def tournament_details(
        self,
        tournament_id: UUID,
        player_id: UUID | None,
        *,
        role: str = "player",
        now: datetime | None = None,
    ) -> TournamentDetails:
        normalized_role = self._normalize_listing_options(role=role)["role"]
        reference_time = self._reference_time(now)
        async with self.database.sessions() as session:
            rows = await self._visible_listing_rows(
                session,
                player_id=player_id,
                role=normalized_role,
                tournament_id=tournament_id,
            )
            if not rows:
                raise LookupError("Tournament not found")
            row = rows[0]
            item = await self._visible_list_item(
                session, row, reference_time, normalized_role if player_id else "guest"
            )
            requirements = tuple(
                (
                    await session.execute(
                        select(TournamentRegistrationRequirementRecord)
                        .where(
                            TournamentRegistrationRequirementRecord.tournament_id == tournament_id,
                            TournamentRegistrationRequirementRecord.revoked_at.is_(None),
                        )
                        .order_by(TournamentRegistrationRequirementRecord.created_at)
                    )
                ).scalars()
            )
            return TournamentDetails(
                tournament=item,
                registration_requirements=tuple(
                    [await self._requirement_snapshot(session, value) for value in requirements]
                ),
                policies=dict(row[3].policies),
                default_parameters=dict(row[3].default_parameters),
                player_mutable_parameters=tuple(row[3].player_mutable_parameters),
            )

    async def manager_settings(
        self, tournament_id: UUID, manager_id: UUID
    ) -> TournamentManagerSettings:
        async with self.database.sessions() as session:
            await self._require_manager(session, tournament_id, manager_id)
            return await self._manager_settings_snapshot(session, tournament_id, manager_id)

    async def search_registered_authors(
        self, tournament_id: UUID, manager_id: UUID, *, query: str = "", limit: int = 20
    ) -> tuple[ManagerAuthorDescriptor, ...]:
        normalized_query = " ".join(query.strip().split())
        if len(normalized_query) > 300 or not 1 <= limit <= 100:
            raise ValueError("Invalid author search")
        async with self.database.sessions() as session:
            await self._require_manager(session, tournament_id, manager_id)
            statement = select(AuthorRecord)
            if normalized_query:
                escaped = normalized_query.casefold().replace("\\", "\\\\")
                escaped = escaped.replace("%", "\\%").replace("_", "\\_")
                statement = statement.where(
                    func.lower(AuthorRecord.display_name).like(f"%{escaped}%", escape="\\")
                )
            authors = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            func.lower(AuthorRecord.display_name), AuthorRecord.id
                        ).limit(limit)
                    )
                ).scalars()
            )
            return tuple(
                ManagerAuthorDescriptor(author.id, author.display_name) for author in authors
            )

    async def register_tournament_author(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        first_name: str,
        second_name: str | None,
        surname: str,
        telegram_link: str | None,
    ) -> TournamentManagerSettings:
        await self.create_tournament_author(
            tournament_id, manager_id, first_name=first_name,
            second_name=second_name, surname=surname, telegram_link=telegram_link,
        )
        return await self.manager_settings(tournament_id, manager_id)

    async def create_tournament_author(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        first_name: str,
        second_name: str | None,
        surname: str,
        telegram_link: str | None,
        packet_draft_id: UUID | None = None,
    ) -> ManagerAuthorDescriptor:
        first = self._normalize_author_component(first_name, "Author name")
        second = (
            self._normalize_author_component(second_name, "Author second name")
            if second_name and second_name.strip()
            else None
        )
        family = self._normalize_author_component(surname, "Author surname")
        link, username = self._normalize_telegram_link(telegram_link)
        display_name = " ".join(part for part in (first, second, family) if part)
        async with self.database.transaction() as session:
            if packet_draft_id is None:
                await self._require_manager(session, tournament_id, manager_id)
            else:
                from sitg_bot.services.packets import PacketAdminService
                from sitg_bot.storage.models import PacketDraftRecord

                draft = await session.get(PacketDraftRecord, packet_draft_id, with_for_update=True)
                if draft is None or draft.creation_tournament_id != tournament_id:
                    raise LookupError("Packet draft not found")
                context = await self.context(session, tournament_id)
                await PacketAdminService(self.database)._require_draft_access(
                    session, context, draft, manager_id,
                )
                if draft.status not in {"awaiting_confirmation", "validation_failed"}:
                    raise ValueError("Only an unpublished packet draft can be edited")
            await self.require_modifiable(session, tournament_id)
            tournament = await session.get(TournamentRecord, tournament_id)
            if tournament is None or tournament.status != "active":
                raise LookupError("Active tournament not found")
            author = AuthorRecord(
                display_name=display_name,
                first_name=first,
                second_name=second,
                surname=family,
                telegram_link=link,
                telegram_username=username,
            )
            session.add(author)
            await session.flush()
            session.add(
                TournamentAuthorRecord(
                    tournament_id=tournament_id,
                    author_id=author.id,
                    added_by_id=manager_id,
                )
            )
            tournament.settings_version += 1
            if username is not None:
                player = await session.scalar(
                    select(PlayerRecord)
                    .where(
                        func.lower(PlayerRecord.telegram_username) == username.casefold(),
                        PlayerRecord.status == "active",
                    )
                    .order_by(
                        PlayerRecord.telegram_username_updated_at.desc().nullslast(),
                        PlayerRecord.id,
                    )
                    .limit(1)
                )
                if player is not None:
                    notification_key = f"tournament-author:{tournament_id}:{author.id}"
                    payload = {
                        "author_id": str(author.id),
                        "author_name": author.display_name,
                        "tournament_id": str(tournament.id),
                        "tournament_name": tournament.name,
                        "_locale": player.preferred_locale,
                    }
                    await NotificationWriter.create_for_player(
                        session,
                        recipient_player_id=player.id,
                        audience="player",
                        kind="author.registered",
                        deduplication_key=notification_key,
                        payload=payload,
                    )
            await session.flush()
            return ManagerAuthorDescriptor(author.id, author.display_name)

    async def update_manager_settings(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        expected_version: int,
        name: str,
        slug: str,
        type_key: str,
        game_ruleset_key: str,
        visibility: str,
        language: str,
        payment_type: str,
        pricing_plans: object,
        registration_open: bool,
        registration_starts_at: datetime | None,
        registration_ends_at: datetime | None,
        starts_at: datetime | None,
        planned_ends_at: datetime | None,
        author_names: tuple[str, ...],
        default_parameters: dict[str, object],
        player_mutable_parameters: set[str] | frozenset[str],
        policies: dict[str, object],
        description: str = "",
        organizer_contacts: str = "",
        channel: str = "",
        ignore_late_registrations: bool = True,
        author_ids: tuple[UUID, ...] = (),
        registration_open_override: bool | None = None,
        registration_requirements: tuple[dict, ...] | None = None,
    ) -> TournamentManagerSettings:
        normalized_name = name.strip()
        normalized_slug = slug.strip().casefold()
        if not normalized_name or len(normalized_name) > 300:
            raise ValueError("Tournament name must contain at most 300 characters")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized_slug):
            raise ValueError("Tournament slug must contain lowercase letters, numbers, and hyphens")
        normalized_visibility = visibility.strip().casefold()
        if normalized_visibility not in {"public", "private"}:
            raise ValueError("Tournament visibility must be public or private")
        if not isinstance(registration_open, bool):
            raise ValueError("registration_open must be a boolean")
        if not isinstance(ignore_late_registrations, bool):
            raise ValueError("ignore_late_registrations must be a boolean")
        self._validate_schedule(
            registration_starts_at, registration_ends_at, starts_at, planned_ends_at
        )
        normalized_payment_type, normalized_pricing_plans = self._pricing(
            payment_type, pricing_plans
        )
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await session.scalar(
                select(TournamentRecord)
                .where(TournamentRecord.id == tournament_id)
                .with_for_update()
            )
            if tournament is None or tournament.status != "active":
                raise LookupError("Active tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")

            if registration_requirements is not None:
                await self._replace_registration_requirements(
                    session, tournament_id, manager_id, registration_requirements
                )

            current_type = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            current_ruleset = await session.get(
                GameRulesetVersionRecord, tournament.game_ruleset_version_id
            )
            assert current_type is not None and current_ruleset is not None
            requested_type = type_key.strip().casefold()
            requested_ruleset = game_ruleset_key.strip().casefold()
            identity_changed = (
                requested_type != current_type.key or requested_ruleset != current_ruleset.key
            )
            if tournament.finalized_at is not None and identity_changed:
                raise ValueError("Tournament type and ruleset cannot change after finalization")
            type_version = (
                current_type
                if requested_type == current_type.key
                else await self._latest_type(session, requested_type)
            )
            ruleset_version = (
                current_ruleset
                if requested_ruleset == current_ruleset.key
                else await self._latest_ruleset(session, requested_ruleset)
            )
            compatible = type_version.rules.get("compatible_rulesets")
            if compatible and ruleset_version.key not in compatible:
                raise ValueError("Tournament type is incompatible with the selected game ruleset")
            if (
                tournament.finalized_at is not None
                and type_version.rules.get("open_ended") is not True
                and registration_ends_at is None
            ):
                raise ValueError("Registration end is required for a finite tournament")
            ruleset = self.rulesets.get(ruleset_version.key, ruleset_version.version)
            default_parameters, player_mutable_parameters = tournament_parameters(
                type_version.key, ruleset_version.key, default_parameters, player_mutable_parameters
            )
            settings, stored_defaults = ruleset_default_settings(ruleset, default_parameters)
            mutable = frozenset(player_mutable_parameters)
            unknown = mutable - ruleset.parameter_names
            if unknown:
                raise ValueError(f"Unknown mutable parameter: {sorted(unknown)[0]}")
            duplicate_slug = await session.scalar(
                select(TournamentRecord.id).where(
                    TournamentRecord.slug == normalized_slug,
                    TournamentRecord.id != tournament_id,
                )
            )
            if duplicate_slug is not None:
                raise ValueError("Tournament slug is already in use")

            tournament.name = normalized_name
            tournament.slug = normalized_slug
            tournament.description = self._normalize_description(description)
            tournament.organizer_contacts = self._normalize_optional_text(
                organizer_contacts, "organizer contacts", max_length=2000
            )
            tournament.channel = self._normalize_optional_text(
                channel, "channel", max_length=500
            )
            tournament.type_version_id = type_version.id
            tournament.game_ruleset_version_id = ruleset_version.id
            tournament.visibility = normalized_visibility
            tournament.language = normalize_language_tag(language)
            tournament.payment_type = normalized_payment_type
            if tournament.registration_open != registration_open:
                tournament.registration_open_override = None
            tournament.registration_open = registration_open
            if registration_open_override is not None:
                if not isinstance(registration_open_override, bool):
                    raise ValueError("Registration override must be boolean")
                if tournament.finalized_at is None:
                    raise ValueError("Finalize tournament setup before changing availability")
                tournament.registration_open_override = registration_open_override
                tournament.registration_open = False
            tournament.ignore_late_registrations = ignore_late_registrations
            tournament.registration_starts_at = registration_starts_at
            tournament.registration_ends_at = registration_ends_at
            tournament.starts_at = starts_at
            tournament.planned_ends_at = planned_ends_at
            tournament.settings_version += 1
            await self._replace_pricing_plans(
                session,
                tournament_id,
                normalized_pricing_plans,
                actor_id=manager_id,
            )
            current_policy = await session.scalar(
                select(TournamentPolicyVersionRecord)
                .where(TournamentPolicyVersionRecord.tournament_id == tournament_id)
                .order_by(TournamentPolicyVersionRecord.version.desc())
                .limit(1)
            )
            assert current_policy is not None
            requested_weight = policies.get(RULESET_RATING_WEIGHT_POLICY, _UNSET)
            current_weight = current_policy.policies.get(RULESET_RATING_WEIGHT_POLICY, 1)
            if requested_weight is not _UNSET and requested_weight != current_weight:
                administrator = await session.get(PlatformAdministratorRecord, manager_id)
                if administrator is None or administrator.revoked_at is not None:
                    raise PermissionError(
                        "Only platform administrators can change rating weighting"
                    )
            manager_policies = {
                key: value for key, value in policies.items() if key != RULESET_RATING_WEIGHT_POLICY
            }
            manager_policies[RULESET_RATING_WEIGHT_POLICY] = (
                current_weight if requested_weight is _UNSET else requested_weight
            )
            AppealPolicy.from_mapping(manager_policies)
            normalized_policies = normalize_tournament_policies(
                type_version.rules, manager_policies
            )
            if author_ids:
                await self._replace_tournament_authors_by_id(
                    session, tournament_id, author_ids, actor_id=manager_id
                )
            else:
                await self._replace_tournament_authors(
                    session, tournament_id, author_names, actor_id=manager_id
                )
            session.add(
                TournamentPolicyVersionRecord(
                    tournament_id=tournament_id,
                    version=current_policy.version + 1,
                    default_parameters=stored_defaults,
                    player_mutable_parameters=sorted(mutable),
                    policies=normalized_policies,
                    created_by_id=manager_id,
                )
            )
            await self._invalidate_assembling_lobbies(session, tournament_id)
            await session.flush()
            return await self._manager_settings_snapshot(session, tournament_id, manager_id)

    async def finalize_tournament_setup(
        self, tournament_id: UUID, manager_id: UUID, *, expected_version: int
    ) -> TournamentManagerSettings:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await session.scalar(
                select(TournamentRecord)
                .where(TournamentRecord.id == tournament_id)
                .with_for_update()
            )
            if tournament is None or tournament.status != "active":
                raise LookupError("Active tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if tournament.finalized_at is not None:
                raise ValueError("Tournament is already finalized")
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            ruleset_version = await session.get(
                GameRulesetVersionRecord, tournament.game_ruleset_version_id
            )
            assert type_version is not None and ruleset_version is not None
            compatible = type_version.rules.get("compatible_rulesets")
            if compatible and ruleset_version.key not in compatible:
                raise ValueError("Tournament type is incompatible with the selected game ruleset")
            if (
                type_version.rules.get("open_ended") is not True
                and tournament.registration_ends_at is None
            ):
                raise ValueError("Registration end is required for a finite tournament")
            tournament.finalized_at = datetime.now(UTC)
            tournament.settings_version += 1
            await session.flush()
            return await self._manager_settings_snapshot(session, tournament_id, manager_id)

    async def invite(self, tournament_id: UUID, player_id: UUID, *, invited_by_id: UUID) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, invited_by_id)
            await self.require_modifiable(session, tournament_id)
            await self._active_tournament(session, tournament_id)
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            if await self._is_manager(session, tournament_id, player_id):
                raise PermissionError("Tournament managers cannot be invited as players")
            membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            if membership is None:
                session.add(
                    TournamentMembershipRecord(
                        tournament_id=tournament_id,
                        player_id=player_id,
                        status="invited",
                        enrolled_by_id=invited_by_id,
                    )
                )
            elif membership.status not in {"active", "approved", "registered"}:
                membership.status = "invited"
                membership.enrolled_by_id = invited_by_id

    async def registration_link(self, tournament_id: UUID, player_id: UUID) -> dict[str, str]:
        async with self.database.sessions() as session:
            tournament = await session.get(TournamentRecord, tournament_id)
            if tournament is None:
                raise LookupError("Tournament not found")
            membership = await session.get(
                TournamentMembershipRecord, (tournament_id, player_id),
            )
            if not (
                await self._is_manager(session, tournament_id, player_id)
                or (membership is not None and membership.status == "active")
            ):
                raise PermissionError("Tournament manager or participant role is required")
            return {
                "name": tournament.name, "reference": f"reg_{tournament.registration_code}",
                "visibility": tournament.visibility,
            }

    @staticmethod
    async def _invitation_tournament(
        session: AsyncSession, reference: str,
    ) -> TournamentRecord:
        match = re.fullmatch(r"(reg|join)_([A-Za-z0-9_-]{32})", reference)
        if match is None:
            raise LookupError("Invitation not found")
        kind, code = match.groups()
        if kind == "reg":
            tournament = await session.scalar(select(TournamentRecord).where(
                TournamentRecord.registration_code == code,
            ))
        else:
            lobby = await session.scalar(select(PregameLobbyRecord).where(
                PregameLobbyRecord.invitation_code == code,
            ))
            if (
                lobby is None or lobby.status != "assembling"
                or lobby.expires_at <= datetime.now(UTC)
            ):
                raise LookupError("Invitation not found")
            tournament = await session.get(TournamentRecord, lobby.tournament_id)
        if tournament is None or tournament.finalized_at is None:
            raise LookupError("Finalized tournament not found")
        return tournament

    async def registration_invitation(
        self, reference: str, player_id: UUID,
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            tournament = await self._invitation_tournament(session, reference)
            membership = await session.get(
                TournamentMembershipRecord, (tournament.id, player_id),
            )
            manager = await self._is_manager(session, tournament.id, player_id)
            return {
                "tournament_id": str(tournament.id), "name": tournament.name,
                "registration_open": self._registration_is_open(tournament, datetime.now(UTC)),
                "membership_status": membership.status if membership else None,
                "is_manager": manager,
            }

    async def register(
        self, tournament_id: UUID, player_id: UUID, *, invitation_reference: str | None = None,
    ) -> RegistrationDecision:
        async with self.database.transaction() as session:
            tournament = await self._active_tournament(session, tournament_id)
            await session.refresh(tournament, with_for_update=True)
            if tournament.finalized_at is None:
                raise LookupError("Finalized tournament not found")
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            if await self._is_manager(session, tournament_id, player_id):
                raise PermissionError("Tournament managers cannot register as players")
            membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            invited = membership is not None and (
                membership.status == "invited" or membership.enrolled_by_id is not None
            )
            if invitation_reference is not None:
                invitation = await self._invitation_tournament(session, invitation_reference)
                if invitation.id != tournament_id:
                    raise PermissionError("Invitation belongs to another tournament")
                invited = True
            if tournament.visibility != "public" and not invited:
                raise PermissionError("A private tournament requires an invitation")
            if not self._registration_is_open(tournament, datetime.now(UTC)):
                raise ValueError("Tournament registration is closed")
            now = datetime.now(UTC)
            if membership is not None and membership.status in {"active", "approved", "registered"}:
                raise ValueError("Player already has a current tournament registration")
            requirements, failures = await self._evaluate_registration_requirements(
                session, tournament_id, player_id
            )
            if membership is None:
                membership = TournamentMembershipRecord(
                    tournament_id=tournament_id,
                    player_id=player_id,
                    registered_at=now,
                )
                session.add(membership)
            else:
                membership.registered_at = now
                membership.approved_at = None
                membership.approved_by_id = None
                membership.participation_confirmed_at = None
                membership.participation_confirmed_by_id = None
            membership.status = "rejected" if failures else "registered"
            if not failures:
                context = await self.context(session, tournament_id)
                if context.policies.get(AUTO_APPROVE_REGISTRATIONS_POLICY) is True:
                    if context.type_key == "classic" and any(
                        stage.started_at
                        for stage in await ClassicService.stages(session, tournament_id)
                    ):
                        raise ValueError("Classic participants are locked after a stage starts")
                    membership.approved_at = now
                    if context.type_rules.get("open_ended") is True:
                        membership.status = "active"
                        membership.participation_confirmed_at = now
                    else:
                        membership.status = "approved"
            membership.registration_rejected_at = now if failures else None
            membership.registration_rejection_reasons = list(failures)
            session.add(
                TournamentRegistrationAttemptRecord(
                    tournament_id=tournament_id,
                    player_id=player_id,
                    accepted=not failures,
                    evaluated_requirement_ids=[str(item.id) for item in requirements],
                    failure_reasons=list(failures),
                )
            )
            return RegistrationDecision(
                accepted=not failures,
                status=membership.status,
                reasons=failures,
            )

    async def _replace_registration_requirements(
        self, session, tournament_id: UUID, manager_id: UUID, requirements: tuple[dict, ...]
    ) -> None:
        requested = {}
        for item in requirements:
            kind = item["kind"]
            target_id = UUID(str(item["target_id"]))
            message = (item.get("failure_message") or "").strip() or None
            if kind not in REGISTRATION_REQUIREMENT_KINDS:
                raise ValueError("Unknown registration requirement")
            if message and len(message) > 500:
                raise ValueError("Registration failure message supports at most 500 characters")
            tournament_target = kind.endswith("tournament")
            if tournament_target and target_id == tournament_id:
                raise ValueError("A participation requirement must target another tournament")
            model = TournamentRecord if tournament_target else LogicalPacketRecord
            if await session.get(model, target_id) is None:
                raise LookupError("Requirement target not found")
            key = (kind, target_id)
            if key in requested:
                raise ValueError("This registration requirement is already active")
            requested[key] = message
        existing = await session.scalars(
            select(TournamentRegistrationRequirementRecord).where(
                TournamentRegistrationRequirementRecord.tournament_id == tournament_id,
                TournamentRegistrationRequirementRecord.revoked_at.is_(None),
            )
        )
        for record in existing:
            key = (record.kind, record.target_tournament_id or record.target_packet_id)
            if key in requested and requested[key] == record.failure_message:
                del requested[key]
            else:
                record.revoked_at = datetime.now(UTC)
                record.revoked_by_id = manager_id
        await session.flush()
        for (kind, target_id), message in requested.items():
            session.add(TournamentRegistrationRequirementRecord(
                tournament_id=tournament_id, kind=kind,
                target_tournament_id=target_id if kind.endswith("tournament") else None,
                target_packet_id=target_id if kind == "has-not-seen-packet" else None,
                failure_message=message, created_by_id=manager_id,
            ))

    async def add_registration_requirement(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        kind: str,
        target_id: UUID,
        failure_message: str | None = None,
    ) -> UUID:
        normalized_kind = kind.strip().casefold().replace("_", "-")
        if normalized_kind not in REGISTRATION_REQUIREMENT_KINDS:
            raise ValueError(f"Unknown registration requirement: {kind}")
        message = failure_message.strip() if failure_message else None
        if message is not None and len(message) > 500:
            raise ValueError("Registration failure message supports at most 500 characters")
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            tournament = await self.require_modifiable(session, tournament_id)
            await self._active_tournament(session, tournament_id)
            target_tournament_id: UUID | None = None
            target_packet_id: UUID | None = None
            if normalized_kind.endswith("tournament"):
                if target_id == tournament_id:
                    raise ValueError("A participation requirement must target another tournament")
                if await session.get(TournamentRecord, target_id) is None:
                    raise LookupError("Requirement target tournament not found")
                target_tournament_id = target_id
            else:
                if await session.get(LogicalPacketRecord, target_id) is None:
                    raise LookupError("Requirement target packet not found")
                target_packet_id = target_id
            duplicate = await session.scalar(
                select(TournamentRegistrationRequirementRecord.id).where(
                    TournamentRegistrationRequirementRecord.tournament_id == tournament_id,
                    TournamentRegistrationRequirementRecord.kind == normalized_kind,
                    TournamentRegistrationRequirementRecord.target_tournament_id
                    == target_tournament_id,
                    TournamentRegistrationRequirementRecord.target_packet_id == target_packet_id,
                    TournamentRegistrationRequirementRecord.revoked_at.is_(None),
                )
            )
            if duplicate is not None:
                raise ValueError("This registration requirement is already active")
            requirement = TournamentRegistrationRequirementRecord(
                tournament_id=tournament_id,
                kind=normalized_kind,
                target_tournament_id=target_tournament_id,
                target_packet_id=target_packet_id,
                failure_message=message,
                created_by_id=manager_id,
            )
            session.add(requirement)
            tournament.settings_version += 1
            await session.flush()
            return requirement.id

    async def remove_registration_requirement(
        self, tournament_id: UUID, requirement_id: UUID, *, manager_id: UUID
    ) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            tournament = await self.require_modifiable(session, tournament_id)
            requirement = await session.get(TournamentRegistrationRequirementRecord, requirement_id)
            if (
                requirement is None
                or requirement.tournament_id != tournament_id
                or requirement.revoked_at is not None
            ):
                raise LookupError("Active registration requirement not found")
            requirement.revoked_at = datetime.now(UTC)
            requirement.revoked_by_id = manager_id
            tournament.settings_version += 1

    async def registration_requirements(
        self,
        tournament_id: UUID,
        *,
        viewer_id: UUID | None = None,
        include_revoked: bool = False,
    ) -> tuple[RegistrationRequirementSnapshot, ...]:
        async with self.database.sessions() as session:
            tournament = await session.get(TournamentRecord, tournament_id)
            if tournament is None:
                raise LookupError("Tournament not found")
            if (
                viewer_id is not None
                and tournament.finalized_at is None
                and not await self._is_manager(session, tournament_id, viewer_id)
            ):
                raise PermissionError("Unfinalized tournament is manager-only")
            if viewer_id is not None and tournament.visibility != "public":
                membership = await session.get(
                    TournamentMembershipRecord, (tournament_id, viewer_id)
                )
                if membership is None:
                    raise PermissionError("Private tournament membership is required")
            query = select(TournamentRegistrationRequirementRecord).where(
                TournamentRegistrationRequirementRecord.tournament_id == tournament_id
            )
            if not include_revoked:
                query = query.where(TournamentRegistrationRequirementRecord.revoked_at.is_(None))
            records = tuple(
                (
                    await session.execute(
                        query.order_by(TournamentRegistrationRequirementRecord.created_at)
                    )
                ).scalars()
            )
            snapshots: list[RegistrationRequirementSnapshot] = []
            for requirement in records:
                snapshots.append(await self._requirement_snapshot(session, requirement))
            return tuple(snapshots)

    async def approve_registration(
        self, tournament_id: UUID, player_id: UUID, *, manager_id: UUID
    ) -> str:
        statuses = await self._approve_registrations(
            tournament_id, manager_id=manager_id, player_id=player_id
        )
        return statuses[player_id]

    async def approve_all_registrations(self, tournament_id: UUID, *, manager_id: UUID) -> int:
        statuses = await self._approve_registrations(tournament_id, manager_id=manager_id)
        return len(statuses)

    async def _approve_registrations(
        self, tournament_id: UUID, *, manager_id: UUID, player_id: UUID | None = None
    ) -> dict[UUID, str]:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await self._active_tournament(session, tournament_id)
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            if type_version.key == "classic":
                await session.refresh(tournament, with_for_update=True)
                if any(s.started_at for s in await ClassicService.stages(session, tournament_id)):
                    raise ValueError("Classic participants are locked after a stage starts")
            query = select(TournamentMembershipRecord).where(
                TournamentMembershipRecord.tournament_id == tournament_id,
                TournamentMembershipRecord.status == "registered",
            )
            if player_id is not None:
                query = query.where(TournamentMembershipRecord.player_id == player_id)
            memberships = list(await session.scalars(query.with_for_update()))
            if player_id is not None and not memberships:
                raise ValueError("Only a pending registration can be approved")
            now = datetime.now(UTC)
            assert type_version is not None
            statuses: dict[UUID, str] = {}
            for membership in memberships:
                if await self._is_manager(session, tournament_id, membership.player_id):
                    raise PermissionError("Tournament managers cannot be approved as players")
                membership.approved_at = now
                membership.approved_by_id = manager_id
                if type_version.rules.get("open_ended") is True:
                    membership.status = "active"
                    membership.participation_confirmed_at = now
                    membership.participation_confirmed_by_id = manager_id
                else:
                    membership.status = "approved"
                statuses[membership.player_id] = membership.status
            if memberships:
                await self._invalidate_assembling_lobbies(session, tournament_id)
            return statuses

    async def reject_registration(
        self, tournament_id: UUID, player_id: UUID, *, manager_id: UUID
    ) -> str:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await self._active_tournament(session, tournament_id)
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            if type_version.key == "classic":
                await session.refresh(tournament, with_for_update=True)
            classic_draft = type_version.key == "classic" and not any(
                s.started_at for s in await ClassicService.stages(session, tournament_id)
            )
            membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            allowed = {"registered", "approved", "active"} if classic_draft else {"registered"}
            if membership is None or membership.status not in allowed:
                raise ValueError("Only a pending registration can be rejected")
            membership.status = "rejected"
            membership.registration_rejected_at = datetime.now(UTC)
            membership.registration_rejection_reasons = ["Rejected by a tournament manager."]
            await self._invalidate_assembling_lobbies(session, tournament_id)
            return membership.status

    async def finalize_participants(
        self,
        tournament_id: UUID,
        participant_ids: set[UUID] | frozenset[UUID],
        *,
        manager_id: UUID,
    ) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await self._active_tournament(session, tournament_id)
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            assert type_version is not None
            if type_version.key == "classic":
                await session.refresh(tournament, with_for_update=True)
                if any(s.started_at for s in await ClassicService.stages(session, tournament_id)):
                    raise ValueError("Classic participants are locked after a stage starts")
            if type_version.rules.get("open_ended") is True:
                raise ValueError(
                    "Open-ended tournaments admit players when registration is approved"
                )
            if self._registration_is_open(tournament, datetime.now(UTC)):
                raise ValueError("Close tournament registration before finalizing participants")
            selected = frozenset(participant_ids)
            memberships = tuple(
                (
                    await session.execute(
                        select(TournamentMembershipRecord)
                        .where(TournamentMembershipRecord.tournament_id == tournament_id)
                        .with_for_update()
                    )
                ).scalars()
            )
            eligible = {item.player_id for item in memberships if item.status == "approved"}
            managers = {
                item.player_id
                for item in (
                    await session.execute(
                        select(TournamentManagerRecord).where(
                            TournamentManagerRecord.tournament_id == tournament_id,
                            TournamentManagerRecord.revoked_at.is_(None),
                        )
                    )
                ).scalars()
            }
            maximum = type_version.rules.get("maximum_participants")
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord)
                .where(TournamentPolicyVersionRecord.tournament_id == tournament_id)
                .order_by(TournamentPolicyVersionRecord.version.desc())
                .limit(1)
            )
            policy_maximum = (
                policy.policies.get(MAXIMUM_PARTICIPANTS_POLICY) if policy is not None else None
            )
            limits = tuple(
                value
                for value in (maximum, policy_maximum)
                if isinstance(value, int) and not isinstance(value, bool)
            )
            effective_maximum = min(limits) if limits else None
            if selected & managers:
                raise PermissionError("Tournament managers cannot be finalized as players")
            if effective_maximum is not None and len(selected) > effective_maximum:
                raise ValueError("Final participant list exceeds the tournament type limit")
            unknown = selected - eligible
            if unknown:
                raise ValueError("Final participant list contains an unapproved player")
            now = datetime.now(UTC)
            for membership in memberships:
                if membership.player_id in selected and membership.status == "approved":
                    membership.status = "active"
                    membership.participation_confirmed_at = now
                    membership.participation_confirmed_by_id = manager_id
                elif membership.status in {"registered", "approved"}:
                    membership.status = "rejected"
            tournament.participants_finalized_at = now
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def enroll(
        self, tournament_id: UUID, player_id: UUID, *, enrolled_by_id: UUID | None = None
    ) -> None:
        async with self.database.transaction() as session:
            if enrolled_by_id is not None:
                await self._require_manager(session, tournament_id, enrolled_by_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await self._active_tournament(session, tournament_id)
            del tournament
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            if await self._is_manager(session, tournament_id, player_id):
                raise PermissionError("Tournament managers cannot be enrolled as players")
            membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            now = datetime.now(UTC)
            if membership is None:
                session.add(
                    TournamentMembershipRecord(
                        tournament_id=tournament_id,
                        player_id=player_id,
                        enrolled_by_id=enrolled_by_id,
                        registered_at=now,
                        approved_at=now,
                        approved_by_id=enrolled_by_id,
                        participation_confirmed_at=now,
                        participation_confirmed_by_id=enrolled_by_id,
                    )
                )
            else:
                membership.status = "active"
                membership.approved_at = membership.approved_at or now
                membership.approved_by_id = enrolled_by_id
                membership.participation_confirmed_at = now
                membership.participation_confirmed_by_id = enrolled_by_id
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def add_manager(
        self, tournament_id: UUID, player_id: UUID, *, granted_by_id: UUID
    ) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, granted_by_id)
            await self.require_modifiable(session, tournament_id)
            manager = await session.get(TournamentManagerRecord, (tournament_id, player_id))
            if manager is not None and manager.revoked_at is None:
                return
            membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            if membership is None or membership.status != "active":
                raise PermissionError("A tournament manager must be an active member")
            if await self._has_active_participation(session, tournament_id, player_id):
                raise ValueError("An active tournament participant cannot become a manager")
            if manager is None:
                session.add(
                    TournamentManagerRecord(
                        tournament_id=tournament_id,
                        player_id=player_id,
                        granted_by_id=granted_by_id,
                    )
                )
            else:
                manager.granted_by_id = granted_by_id
                manager.revoked_at = None
            membership.status = "left"
            # The new manager can now read every packet assigned to the tournament.
            assignments = (
                await session.scalars(
                    select(TournamentPacketAssignmentRecord).where(
                        TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                        TournamentPacketAssignmentRecord.status == "active",
                    )
                )
            ).all()
            for item in assignments:
                version_id = item.adopted_version_id or await session.scalar(
                    select(PacketVersionRecord.id)
                    .where(
                        PacketVersionRecord.packet_id == item.packet_id,
                        PacketVersionRecord.state == "published",
                    )
                    .order_by(PacketVersionRecord.version_number.desc())
                    .limit(1)
                )
                if version_id is not None:
                    await burn_author_content(
                        session, version_id=version_id, player_ids=(player_id,)
                    )
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def remove_manager(
        self, tournament_id: UUID, player_id: UUID, *, removed_by_id: UUID
    ) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, removed_by_id)
            await self.require_modifiable(session, tournament_id)
            managers = tuple(
                (
                    await session.execute(
                        select(TournamentManagerRecord).where(
                            TournamentManagerRecord.tournament_id == tournament_id,
                            TournamentManagerRecord.revoked_at.is_(None),
                        )
                    )
                ).scalars()
            )
            target = next((item for item in managers if item.player_id == player_id), None)
            if target is None:
                raise LookupError("Active tournament manager not found")
            if len(managers) == 1:
                raise ValueError("A tournament must retain at least one manager")
            target.revoked_at = datetime.now(UTC)
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def update_metadata(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        language: str | object = _UNSET,
        payment_type: str | object = _UNSET,
        pricing_plans: object = _UNSET,
        registration_open: bool | object = _UNSET,
        ignore_late_registrations: bool | object = _UNSET,
        registration_starts_at: datetime | None | object = _UNSET,
        registration_ends_at: datetime | None | object = _UNSET,
        starts_at: datetime | None | object = _UNSET,
        planned_ends_at: datetime | None | object = _UNSET,
        description: str | object = _UNSET,
        author_names: tuple[str, ...] | object = _UNSET,
    ) -> None:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await session.scalar(
                select(TournamentRecord)
                .where(
                    TournamentRecord.id == tournament_id,
                    TournamentRecord.status == "active",
                )
                .with_for_update()
            )
            if tournament is None:
                raise LookupError("Active tournament not found")
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            assert type_version is not None
            new_registration_start = (
                tournament.registration_starts_at
                if registration_starts_at is _UNSET
                else registration_starts_at
            )
            new_registration_end = (
                tournament.registration_ends_at
                if registration_ends_at is _UNSET
                else registration_ends_at
            )
            new_start = tournament.starts_at if starts_at is _UNSET else starts_at
            new_planned_end = (
                tournament.planned_ends_at if planned_ends_at is _UNSET else planned_ends_at
            )
            assert new_registration_start is None or isinstance(new_registration_start, datetime)
            assert new_registration_end is None or isinstance(new_registration_end, datetime)
            assert new_start is None or isinstance(new_start, datetime)
            assert new_planned_end is None or isinstance(new_planned_end, datetime)
            self._validate_schedule(
                new_registration_start, new_registration_end, new_start, new_planned_end
            )
            if (
                tournament.finalized_at is not None
                and type_version.rules.get("open_ended") is not True
                and new_registration_end is None
            ):
                raise ValueError("Registration end is required for a finite tournament")

            if payment_type is not _UNSET or pricing_plans is not _UNSET:
                raw_payment_type = (
                    tournament.payment_type if payment_type is _UNSET else str(payment_type)
                )
                existing_plans = await self.pricing_plans(session, tournament_id)
                raw_plans: object
                if pricing_plans is not _UNSET:
                    raw_plans = pricing_plans
                elif raw_payment_type.strip().casefold().replace("_", "-") == "free":
                    raw_plans = ()
                else:
                    raw_plans = [
                        {
                            "name": plan.name,
                            "prices": [
                                {"amount": price.amount, "currency": price.currency}
                                for price in plan.prices
                            ],
                        }
                        for plan in existing_plans
                    ]
                normalized_payment_type, normalized_pricing_plans = self._pricing(
                    raw_payment_type, raw_plans
                )
                tournament.payment_type = normalized_payment_type
                if pricing_plans is not _UNSET or normalized_payment_type == "free":
                    await self._replace_pricing_plans(
                        session,
                        tournament_id,
                        normalized_pricing_plans,
                        actor_id=manager_id,
                    )
            if language is not _UNSET:
                tournament.language = normalize_language_tag(str(language))
            if description is not _UNSET:
                tournament.description = self._normalize_description(description)
            if registration_open is not _UNSET:
                if not isinstance(registration_open, bool):
                    raise ValueError("registration_open must be a boolean")
                if tournament.registration_open != registration_open:
                    tournament.registration_open_override = None
                tournament.registration_open = registration_open
            if ignore_late_registrations is not _UNSET:
                if not isinstance(ignore_late_registrations, bool):
                    raise ValueError("ignore_late_registrations must be a boolean")
                tournament.ignore_late_registrations = ignore_late_registrations
            tournament.registration_starts_at = new_registration_start
            tournament.registration_ends_at = new_registration_end
            tournament.starts_at = new_start
            tournament.planned_ends_at = new_planned_end

            if author_names is not _UNSET:
                names = tuple(
                    dict.fromkeys(
                        self._normalize_author_name(name) for name in author_names if name.strip()
                    )
                )
                existing_rows = tuple(
                    (
                        await session.execute(
                            select(TournamentAuthorRecord, AuthorRecord)
                            .join(
                                AuthorRecord,
                                AuthorRecord.id == TournamentAuthorRecord.author_id,
                            )
                            .where(TournamentAuthorRecord.tournament_id == tournament_id)
                        )
                    ).all()
                )
                existing_by_name: dict[str, list[TournamentAuthorRecord]] = {}
                for record, author in existing_rows:
                    existing_by_name.setdefault(author.display_name, []).append(record)
                for name in names:
                    matching = existing_by_name.get(name, [])
                    if len(matching) > 1:
                        raise ValueError(
                            "Tournament authors with the same name cannot be safely edited by name"
                        )
                    if matching:
                        matching.pop()
                        continue
                    author = await self._create_author(session, name)
                    session.add(
                        TournamentAuthorRecord(
                            tournament_id=tournament_id,
                            author_id=author.id,
                            added_by_id=manager_id,
                        )
                    )
                for records in existing_by_name.values():
                    for record in records:
                        await session.delete(record)
            tournament.settings_version += 1
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def complete_tournament(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        actual_ends_at: datetime | None = None,
        expected_version: int | None = None,
    ) -> None:
        finished_at = actual_ends_at or datetime.now(UTC)
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("actual_ends_at must include a timezone")
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await self._active_tournament(session, tournament_id)
            if expected_version is not None and tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if tournament.finalized_at is None:
                raise ValueError("Finalize tournament setup before marking it finished")
            if (
                tournament.actual_starts_at is not None
                and finished_at < tournament.actual_starts_at
            ):
                raise ValueError("Actual finish cannot precede tournament start")
            tournament.actual_ends_at = finished_at
            tournament.status = "completed"
            tournament.registration_open = False
            tournament.registration_open_override = False
            tournament.settings_version += 1
            await self._invalidate_assembling_lobbies(session, tournament_id)

    async def start_tournament(
        self, tournament_id: UUID, manager_id: UUID, *, expected_version: int,
    ) -> TournamentManagement:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await session.get(TournamentRecord, tournament_id, with_for_update=True)
            if tournament is None or tournament.status != "active":
                raise LookupError("Active tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if tournament.finalized_at is None:
                raise ValueError("Finalize tournament setup before starting it")
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            if type_version.key == "classic":
                raise ValueError("Start a Classic stage to start the tournament")
            if tournament.actual_starts_at is not None:
                raise ValueError("Tournament has already started")
            tournament.actual_starts_at = datetime.now(UTC)
            tournament.settings_version += 1
            await self._invalidate_assembling_lobbies(session, tournament_id)
            await session.flush()
            return await self._manager_management_snapshot(session, tournament_id, manager_id)

    async def remind_scheduled_starts(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            tournaments = list(await session.scalars(
                select(TournamentRecord).where(
                    TournamentRecord.status == "active",
                    TournamentRecord.actual_starts_at.is_(None),
                    TournamentRecord.starts_at <= now,
                    TournamentRecord.start_reminded_for.is_distinct_from(TournamentRecord.starts_at),
                ).with_for_update(skip_locked=True)
            ))
            for tournament in tournaments:
                managers = await session.scalars(
                    select(TournamentManagerRecord.player_id).where(
                        TournamentManagerRecord.tournament_id == tournament.id,
                        TournamentManagerRecord.revoked_at.is_(None),
                    ).order_by(TournamentManagerRecord.player_id)
                )
                for manager_id in managers:
                    key = (
                        f"tournament-start:{tournament.id}:"
                        f"{tournament.starts_at.isoformat()}:{manager_id}"
                    )
                    if await session.scalar(select(PlayerNotificationRecord.id).where(
                        PlayerNotificationRecord.deduplication_key == key
                    )):
                        continue
                    await NotificationWriter.create_for_player(
                        session, recipient_player_id=manager_id, audience="manager",
                        kind="tournament.start_due",
                        deduplication_key=key,
                        payload={"tournament_id": str(tournament.id), "name": tournament.name},
                    )
                tournament.start_reminded_for = tournament.starts_at

    async def manager_management(
        self, tournament_id: UUID, manager_id: UUID
    ) -> TournamentManagement:
        async with self.database.sessions() as session:
            await self._require_manager(session, tournament_id, manager_id)
            return await self._manager_management_snapshot(session, tournament_id, manager_id)

    async def set_registration_override(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        registration_open: bool,
        expected_version: int,
    ) -> TournamentManagement:
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            tournament = await session.scalar(
                select(TournamentRecord)
                .where(TournamentRecord.id == tournament_id)
                .with_for_update()
            )
            if tournament is None or tournament.status != "active":
                raise LookupError("Active tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if tournament.finalized_at is None:
                raise ValueError("Finalize tournament setup before opening registration")
            tournament.registration_open = False
            tournament.registration_open_override = registration_open
            tournament.settings_version += 1
            await session.flush()
            return await self._manager_management_snapshot(session, tournament_id, manager_id)

    async def set_management_packet_access(
        self,
        tournament_id: UUID,
        assignment_id: UUID,
        manager_id: UUID,
        *,
        right: str,
        enabled: bool | None = None,
        player_id: UUID | None,
        library_viewing_rule: str | None = None,
        expected_version: int | None = None,
    ) -> TournamentManagement:
        right_map = {
            "playable": ("playable", "playable_by_members"),
            "discoverable": ("discoverable", "discoverable_by_members"),
            "readable": ("content_visible", "content_visible_by_members"),
        }
        if right == "library_viewing_rule":
            if library_viewing_rule not in LIBRARY_VIEWING_RULES or player_id is not None:
                raise ValueError("A valid packet-wide library viewing rule is required")
        elif right not in right_map or not isinstance(enabled, bool):
            raise ValueError("Unknown managed packet right")
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            tournament = await self.require_modifiable(session, tournament_id)
            if right == "library_viewing_rule" and tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord)
                .where(
                    TournamentPacketAssignmentRecord.id == assignment_id,
                    TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                    TournamentPacketAssignmentRecord.status == "active",
                )
                .with_for_update()
            )
            if assignment is None:
                raise LookupError("Active tournament packet assignment not found")
            if right == "library_viewing_rule":
                assignment.library_viewing_rule = library_viewing_rule
                tournament.settings_version += 1
                await session.flush()
                return await self._manager_management_snapshot(session, tournament_id, manager_id)
            entitlement_field, assignment_field = right_map[right]
            participants = tuple(
                (
                    await session.execute(
                        select(TournamentMembershipRecord)
                        .where(
                            TournamentMembershipRecord.tournament_id == tournament_id,
                            TournamentMembershipRecord.status == "active",
                        )
                        .order_by(TournamentMembershipRecord.player_id)
                    )
                ).scalars()
            )
            participant_ids = {membership.player_id for membership in participants}
            if player_id is not None and player_id not in participant_ids:
                raise LookupError("Active tournament participant not found")
            target_ids = participant_ids if player_id is None else {player_id}
            effective = {}
            if right != "playable":
                effective = {
                    membership.player_id: await self.has_assignment_access(
                        session, assignment, membership.player_id, entitlement_field
                    )
                    for membership in participants
                }
                setattr(assignment, assignment_field, False)
                target_ids = participant_ids
            for membership in participants:
                if membership.player_id not in target_ids:
                    continue
                entitlement = await session.get(
                    TournamentPacketEntitlementRecord,
                    (assignment.id, membership.player_id),
                )
                if entitlement is None:
                    entitlement = TournamentPacketEntitlementRecord(
                        assignment_id=assignment.id,
                        player_id=membership.player_id,
                        granted_by_id=manager_id,
                    )
                    session.add(entitlement)
                value = (
                    enabled
                    if player_id is None or player_id == membership.player_id
                    else effective[membership.player_id]
                )
                setattr(entitlement, entitlement_field, value)
                if right in {"discoverable", "readable"}:
                    setattr(entitlement, f"{right}_override", value)
                entitlement.granted_by_id = manager_id
                entitlement.revoked_at = None
            if player_id is None:
                setattr(assignment, assignment_field, enabled)
            await self._invalidate_assembling_lobbies(session, tournament_id)
            await session.flush()
            return await self._manager_management_snapshot(session, tournament_id, manager_id)

    async def tournament_authors(
        self, session: AsyncSession, tournament_id: UUID
    ) -> tuple[str, ...]:
        names = set(
            (
                await session.execute(
                    select(AuthorRecord.display_name)
                    .join(
                        TournamentAuthorRecord,
                        TournamentAuthorRecord.author_id == AuthorRecord.id,
                    )
                    .where(TournamentAuthorRecord.tournament_id == tournament_id)
                )
            ).scalars()
        )
        assignments = tuple(
            (
                await session.execute(
                    select(TournamentPacketAssignmentRecord).where(
                        TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                        TournamentPacketAssignmentRecord.status == "active",
                    )
                )
            ).scalars()
        )
        for assignment in assignments:
            version = (
                await session.get(PacketVersionRecord, assignment.adopted_version_id)
                if assignment.adopted_version_id is not None
                else await session.scalar(
                    select(PacketVersionRecord)
                    .where(
                        PacketVersionRecord.packet_id == assignment.packet_id,
                        PacketVersionRecord.state == "published",
                    )
                    .order_by(PacketVersionRecord.version_number.desc())
                    .limit(1)
                )
            )
            if version is None:
                continue
            author_ids: set[UUID] = set()
            if version.lead_author_id is not None:
                author_ids.add(version.lead_author_id)
            author_ids.update(await session.scalars(packet_author_ids(version.id)))
            if author_ids:
                names.update(
                    (
                        await session.execute(
                            select(AuthorRecord.display_name).where(AuthorRecord.id.in_(author_ids))
                        )
                    ).scalars()
                )
        return tuple(sorted(names, key=str.casefold))

    @staticmethod
    async def pricing_plans(
        session: AsyncSession, tournament_id: UUID
    ) -> tuple[PricingPlanSnapshot, ...]:
        plans = tuple(
            (
                await session.execute(
                    select(TournamentPricingPlanRecord)
                    .where(TournamentPricingPlanRecord.tournament_id == tournament_id)
                    .order_by(TournamentPricingPlanRecord.position)
                )
            ).scalars()
        )
        result: list[PricingPlanSnapshot] = []
        for plan in plans:
            prices = tuple(
                PricingPlanPriceSnapshot(amount=Decimal(price.amount), currency=price.currency)
                for price in (
                    await session.execute(
                        select(TournamentPricingPlanPriceRecord)
                        .where(TournamentPricingPlanPriceRecord.pricing_plan_id == plan.id)
                        .order_by(TournamentPricingPlanPriceRecord.currency)
                    )
                ).scalars()
            )
            result.append(PricingPlanSnapshot(id=plan.id, name=plan.name, prices=prices))
        return tuple(result)

    @staticmethod
    async def _replace_pricing_plans(
        session: AsyncSession,
        tournament_id: UUID,
        pricing_plans: tuple[tuple[str, tuple[PricingPlanPriceSnapshot, ...]], ...],
        *,
        actor_id: UUID,
    ) -> None:
        existing = tuple(
            (
                await session.execute(
                    select(TournamentPricingPlanRecord).where(
                        TournamentPricingPlanRecord.tournament_id == tournament_id
                    )
                )
            ).scalars()
        )
        for plan in existing:
            await session.delete(plan)
        if existing:
            await session.flush()
        for position, (name, prices) in enumerate(pricing_plans, 1):
            plan = TournamentPricingPlanRecord(
                tournament_id=tournament_id,
                name=name,
                position=position,
                created_by_id=actor_id,
            )
            session.add(plan)
            await session.flush()
            session.add_all(
                TournamentPricingPlanPriceRecord(
                    pricing_plan_id=plan.id,
                    amount=price.amount,
                    currency=price.currency,
                )
                for price in prices
            )

    async def _replace_tournament_authors(
        self,
        session: AsyncSession,
        tournament_id: UUID,
        author_names: tuple[str, ...],
        *,
        actor_id: UUID,
    ) -> None:
        names = tuple(
            dict.fromkeys(
                self._normalize_author_name(name) for name in author_names if name.strip()
            )
        )
        existing = tuple(
            (
                await session.execute(
                    select(TournamentAuthorRecord).where(
                        TournamentAuthorRecord.tournament_id == tournament_id
                    )
                )
            ).scalars()
        )
        for record in existing:
            await session.delete(record)
        if existing:
            await session.flush()
        for name in names:
            author = await self._create_author(session, name)
            session.add(
                TournamentAuthorRecord(
                    tournament_id=tournament_id,
                    author_id=author.id,
                    added_by_id=actor_id,
                )
            )

    async def _replace_tournament_authors_by_id(
        self,
        session: AsyncSession,
        tournament_id: UUID,
        author_ids: tuple[UUID, ...],
        *,
        actor_id: UUID,
    ) -> None:
        unique_ids = tuple(dict.fromkeys(author_ids))
        authors = tuple(
            (
                await session.execute(select(AuthorRecord).where(AuthorRecord.id.in_(unique_ids)))
            ).scalars()
        )
        if len(authors) != len(unique_ids):
            raise LookupError("Registered author not found")
        existing = tuple(
            (
                await session.execute(
                    select(TournamentAuthorRecord).where(
                        TournamentAuthorRecord.tournament_id == tournament_id
                    )
                )
            ).scalars()
        )
        for record in existing:
            await session.delete(record)
        if existing:
            await session.flush()
        for author_id in unique_ids:
            session.add(
                TournamentAuthorRecord(
                    tournament_id=tournament_id,
                    author_id=author_id,
                    added_by_id=actor_id,
                )
            )

    async def _manager_settings_snapshot(
        self, session: AsyncSession, tournament_id: UUID, manager_id: UUID
    ) -> TournamentManagerSettings:
        rows = await self._visible_listing_rows(
            session,
            player_id=manager_id,
            role="manager",
            tournament_id=tournament_id,
        )
        if not rows:
            raise LookupError("Managed tournament not found")
        row = rows[0]
        tournament = row[0]
        policy = row[3]
        item = await self._visible_list_item(session, row, datetime.now(UTC), "manager")
        requirements = tuple(
            (
                await session.execute(
                    select(TournamentRegistrationRequirementRecord)
                    .where(
                        TournamentRegistrationRequirementRecord.tournament_id == tournament_id,
                        TournamentRegistrationRequirementRecord.revoked_at.is_(None),
                    )
                    .order_by(TournamentRegistrationRequirementRecord.created_at)
                )
            ).scalars()
        )
        type_options = tuple(
            (
                await session.execute(
                    select(TournamentTypeVersionRecord.key)
                    .where(TournamentTypeVersionRecord.active.is_(True))
                    .distinct()
                    .order_by(TournamentTypeVersionRecord.key)
                )
            ).scalars()
        )
        ruleset_options = tuple(
            (
                await session.execute(
                    select(GameRulesetVersionRecord.key)
                    .where(GameRulesetVersionRecord.active.is_(True))
                    .distinct()
                    .order_by(GameRulesetVersionRecord.key)
                )
            ).scalars()
        )
        author_records = tuple(
            (
                await session.execute(
                    select(AuthorRecord)
                    .join(
                        TournamentAuthorRecord,
                        TournamentAuthorRecord.author_id == AuthorRecord.id,
                    )
                    .where(TournamentAuthorRecord.tournament_id == tournament_id)
                    .order_by(AuthorRecord.display_name)
                )
            ).scalars()
        )
        authors = tuple(
            ManagerAuthorDescriptor(author.id, author.display_name) for author in author_records
        )
        ruleset = self.rulesets.get(item.ruleset_key, item.ruleset_version)
        parameters, mutable = tournament_parameters(
            item.type_key, item.ruleset_key,
            policy.default_parameters, policy.player_mutable_parameters,
        )
        setting_descriptors = tuple(
            ManagerSettingDescriptor(
                definition.name,
                definition.value_type,
                definition.description_key,
                parameters.get(definition.name),
            )
            for definition in ruleset.parameter_definitions
            if not (item.type_key == "classic" and definition.name == "theme_count")
        )
        policy_descriptors = self._manager_policy_descriptors(policy.policies)
        counts = []
        for record_type in (
            TournamentPacketAssignmentRecord,
            TournamentMembershipRecord,
            TournamentManagerRecord,
        ):
            statement = (
                select(func.count())
                .select_from(record_type)
                .where(record_type.tournament_id == tournament_id)
            )
            if record_type in {TournamentPacketAssignmentRecord, TournamentManagerRecord}:
                if record_type is TournamentPacketAssignmentRecord:
                    statement = statement.where(record_type.status == "active")
                else:
                    statement = statement.where(record_type.revoked_at.is_(None))
            counts.append(int(await session.scalar(statement) or 0))
        actions = ["update_metadata", "update_pricing", "update_registration", "update_policy"]
        if tournament.finalized_at is None:
            actions.extend(("update_competition", "finalize"))
        return TournamentManagerSettings(
            tournament=item,
            settings_version=tournament.settings_version,
            finalized_at=tournament.finalized_at,
            registration_enabled=tournament.registration_open,
            ignore_late_registrations=tournament.ignore_late_registrations,
            available_actions=tuple(actions),
            type_options=type_options,
            ruleset_options=ruleset_options,
            policies={
                key: value
                for key, value in policy.policies.items()
                if key != RULESET_RATING_WEIGHT_POLICY
            },
            default_parameters=parameters,
            player_mutable_parameters=tuple(sorted(mutable)),
            author_names=tuple(author.display_name for author in author_records),
            authors=authors,
            setting_descriptors=setting_descriptors,
            policy_descriptors=policy_descriptors,
            registration_requirements=tuple(
                [await self._requirement_snapshot(session, value) for value in requirements]
            ),
            packet_assignment_count=counts[0],
            membership_count=counts[1],
            manager_count=counts[2],
            organizer_contacts=tournament.organizer_contacts,
            channel=tournament.channel,
            classic=await ClassicService(self.database).snapshot(session, tournament_id)
            if item.type_key == "classic"
            else None,
        )

    async def _manager_management_snapshot(
        self, session: AsyncSession, tournament_id: UUID, manager_id: UUID
    ) -> TournamentManagement:
        rows = await self._visible_listing_rows(
            session,
            player_id=manager_id,
            role="manager",
            tournament_id=tournament_id,
        )
        if not rows:
            raise LookupError("Managed tournament not found")
        tournament = rows[0][0]
        item = await self._visible_list_item(session, rows[0], datetime.now(UTC), "manager")
        type_version = await session.get(TournamentTypeVersionRecord, tournament.type_version_id)
        assert type_version is not None

        registration_rows = tuple(
            (
                await session.execute(
                    select(TournamentMembershipRecord, PlayerRecord)
                    .join(PlayerRecord, PlayerRecord.id == TournamentMembershipRecord.player_id)
                    .where(
                        TournamentMembershipRecord.tournament_id == tournament_id,
                        TournamentMembershipRecord.registered_at.is_not(None),
                    )
                    .order_by(
                        TournamentMembershipRecord.registered_at,
                        PlayerRecord.public_nickname,
                        PlayerRecord.id,
                    )
                )
            ).all()
        )
        classic_started = item.type_key == "classic" and any(
            s.started_at for s in await ClassicService.stages(session, tournament_id)
        )
        classic_draft = item.type_key == "classic" and not classic_started
        registrations = tuple(
            ManagementRegistration(
                player_id=membership.player_id,
                display_name=player.public_nickname or "—",
                real_name=player.real_name,
                status=membership.status,
                registered_at=membership.registered_at,
                available_actions=("approve", "reject")
                if membership.status == "registered"
                and tournament.status == "active"
                and not classic_started
                else ("reject",)
                if classic_draft
                and membership.status in {"approved", "active"}
                and tournament.status == "active"
                else (),
            )
            for membership, player in registration_rows
            if membership.registered_at is not None
        )
        participant_rows = tuple(
            (
                await session.execute(
                    select(TournamentMembershipRecord, PlayerRecord)
                    .join(PlayerRecord, PlayerRecord.id == TournamentMembershipRecord.player_id)
                    .where(
                        TournamentMembershipRecord.tournament_id == tournament_id,
                        TournamentMembershipRecord.status == "active",
                    )
                    .order_by(PlayerRecord.public_nickname, PlayerRecord.id)
                )
            ).all()
        )
        assignments = tuple(
            (
                await session.execute(
                    select(TournamentPacketAssignmentRecord)
                    .where(
                        TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                        TournamentPacketAssignmentRecord.status == "active",
                    )
                    .order_by(TournamentPacketAssignmentRecord.created_at)
                )
            ).scalars()
        )
        packets: list[ManagementPacket] = []
        for assignment in assignments:
            version = (
                await session.get(PacketVersionRecord, assignment.adopted_version_id)
                if assignment.adopted_version_id is not None
                else await session.scalar(
                    select(PacketVersionRecord)
                    .where(
                        PacketVersionRecord.packet_id == assignment.packet_id,
                        PacketVersionRecord.state == "published",
                    )
                    .order_by(PacketVersionRecord.version_number.desc())
                    .limit(1)
                )
            )
            player_access: list[ManagementPacketAccess] = []
            for membership, player in participant_rows:
                player_access.append(
                    ManagementPacketAccess(
                        player_id=membership.player_id,
                        display_name=player.public_nickname or "—",
                        playable=await self.has_assignment_access(
                            session, assignment, membership.player_id, "playable"
                        ),
                        discoverable=await self.has_assignment_access(
                            session, assignment, membership.player_id, "discoverable"
                        ),
                        readable=await self.has_assignment_access(
                            session, assignment, membership.player_id, "content_visible"
                        ),
                    )
                )
            packets.append(
                ManagementPacket(
                    assignment_id=assignment.id,
                    packet_id=assignment.packet_id,
                    name=version.name if version is not None else str(assignment.packet_id),
                    version=version.version_number if version is not None else None,
                    player_access=tuple(player_access),
                    year=version.year if version else None,
                    published_at=version.published_at if version else None,
                    lead_author=(await session.scalar(select(AuthorRecord.display_name).where(
                        AuthorRecord.id == version.lead_author_id
                    ))) if version else None,
                    authors=tuple((await session.scalars(
                        select(AuthorRecord.display_name).where(AuthorRecord.id.in_(
                            packet_author_ids(version.id)
                        )).distinct().order_by(AuthorRecord.display_name)
                    )).all()) if version else (),
                    released=bool(version and version.library_released_at),
                    packet_version_id=version.id if version else None,
                    library_viewing_rule=assignment.library_viewing_rule,
                    default_access={
                        "discoverable": assignment.discoverable_by_members,
                        "playable": assignment.playable_by_members,
                        "readable": assignment.content_visible_by_members,
                    },
                )
            )

        configured_sections = type_version.rules.get("manager_management_sections")
        supported_sections = (
            "general",
            "registrations",
            "packet_accessibility",
            "packet_management",
        )
        sections = (
            tuple(
                section
                for section in configured_sections
                if isinstance(section, str) and section in supported_sections
            )
            if isinstance(configured_sections, list)
            else supported_sections
        )
        if item.type_key == "classic":
            sections += ("first_stage", "playoff_stage", "first_round_seeding")
        if item.type_key == "ladder":
            sections += ("subscriptions",)
        scheduled_open = self._scheduled_registration_is_open(tournament, datetime.now(UTC))
        actions: list[str] = ["packet_management"]
        if tournament.status == "active":
            actions.extend(("registration_decide", "packet_access"))
            if item.type_key == "ladder" and tournament.moderation_status == "normal":
                actions.append("subscriptions")
            if tournament.finalized_at is None:
                actions.append("finalize")
            else:
                actions.extend(("registration_override", "mark_finished"))
                if tournament.actual_starts_at is None and item.type_key != "classic":
                    actions.append("start_tournament")
        return TournamentManagement(
            tournament=item,
            sections=sections,
            settings_version=tournament.settings_version,
            finalized_at=tournament.finalized_at,
            registration_scheduled_open=scheduled_open,
            registration_open=self._registration_is_open(tournament, datetime.now(UTC)),
            registration_open_override=tournament.registration_open_override,
            registration_count=len(registrations),
            approved_count=sum(
                registration.status in {"approved", "active"} for registration in registrations
            ),
            participant_count=len(participant_rows),
            packet_count=len(assignments),
            registrations=registrations,
            packets=tuple(packets),
            available_actions=tuple(actions),
            classic=await ClassicService(self.database).snapshot(session, tournament_id)
            if item.type_key == "classic"
            else None,
            subscriptions=await subscription_snapshot(session, tournament_id)
            if item.type_key == "ladder" else None,
        )

    async def update_policy(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        default_parameters: dict[str, object],
        player_mutable_parameters: set[str] | frozenset[str],
        policies: dict[str, object],
    ) -> TournamentContext:
        AppealPolicy.from_mapping(policies)
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            context = await self.context(session, tournament_id, lock=True)
            tournament = await session.get(TournamentRecord, tournament_id)
            assert tournament is not None
            current_weight = context.policies.get(RULESET_RATING_WEIGHT_POLICY, 1)
            requested_weight = policies.get(RULESET_RATING_WEIGHT_POLICY, current_weight)
            if requested_weight != current_weight:
                administrator = await session.get(PlatformAdministratorRecord, manager_id)
                if administrator is None or administrator.revoked_at is not None:
                    raise PermissionError(
                        "Only platform administrators can change rating weighting"
                    )
            effective_policies = dict(policies)
            effective_policies[RULESET_RATING_WEIGHT_POLICY] = requested_weight
            normalized_policies = normalize_tournament_policies(
                context.type_rules, effective_policies
            )
            ruleset = self.rulesets.get(context.ruleset_key, context.ruleset_version)
            default_parameters, player_mutable_parameters = tournament_parameters(
                context.type_key, context.ruleset_key, default_parameters, player_mutable_parameters
            )
            settings, stored_defaults = ruleset_default_settings(ruleset, default_parameters)
            mutable = frozenset(player_mutable_parameters)
            unknown = mutable - ruleset.parameter_names
            if unknown:
                raise ValueError(f"Unknown mutable parameter: {sorted(unknown)[0]}")
            version = context.policy_version + 1
            policy = TournamentPolicyVersionRecord(
                tournament_id=tournament_id,
                version=version,
                default_parameters=stored_defaults,
                player_mutable_parameters=sorted(mutable),
                policies=normalized_policies,
                created_by_id=manager_id,
            )
            session.add(policy)
            tournament.settings_version += 1
            await session.flush()
            lobbies = tuple(
                (
                    await session.execute(
                        select(PregameLobbyRecord)
                        .where(
                            PregameLobbyRecord.tournament_id == tournament_id,
                            PregameLobbyRecord.status == "assembling",
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            old_defaults = context.settings.to_dict()
            for lobby in lobbies:
                retained_overrides = {
                    key: value
                    for key, value in lobby.settings.items()
                    if key in mutable and old_defaults.get(key) != value
                }
                maximum_themes = lobby.settings.get("theme_count") == MAX_THEME_COUNT and (
                    retained_overrides.get("theme_count") == MAX_THEME_COUNT
                    or stored_defaults.get("theme_count") == MAX_THEME_COUNT
                )
                effective = settings.updated(
                    {
                        key: value
                        for key, value in retained_overrides.items()
                        if not (key == "theme_count" and value == MAX_THEME_COUNT)
                    }
                )
                lobby.settings = effective.to_dict()
                if maximum_themes:
                    lobby.settings = {**lobby.settings, "theme_count": MAX_THEME_COUNT}
                if context.type_key != "classic":
                    lobby.max_players = int(lobby.settings["maximum_players"])
                lobby.tournament_policy_version_id = policy.id
                lobby.validation = []
                if not (
                    context.type_supports_hybrid_matchmaking
                    and normalized_policies[HYBRID_MATCHMAKING_POLICY] is True
                ):
                    lobby.searching = False
                    lobby.search_started_at = None
                lobby.version += 1
                lobby.updated_at = datetime.now(UTC)
                members = (
                    await session.execute(
                        select(PregameLobbyMemberRecord).where(
                            PregameLobbyMemberRecord.lobby_id == lobby.id,
                            PregameLobbyMemberRecord.active.is_(True),
                        )
                    )
                ).scalars()
                for member in members:
                    member.ready = False
                    if member.role == "observer":
                        member.fresh_content_confirmed = False
                sequence = (
                    await session.scalar(
                        select(func.max(PregameLobbyEventRecord.sequence)).where(
                            PregameLobbyEventRecord.lobby_id == lobby.id
                        )
                    )
                    or 0
                )
                session.add(
                    PregameLobbyEventRecord(
                        lobby_id=lobby.id,
                        sequence=sequence + 1,
                        kind="tournament_policy_changed",
                        payload={"policy_version": version},
                    )
                )
                await TransactionalOutbox.enqueue_lobby_event(
                    session,
                    lobby_id=lobby.id,
                    sequence=sequence + 1,
                    kind="tournament_policy_changed",
                    parameters={"policy_version": version},
                )
            return TournamentContext(
                context.tournament_id,
                context.type_version_id,
                context.type_key,
                context.game_ruleset_version_id,
                context.ruleset_key,
                context.ruleset_version,
                policy.id,
                version,
                settings,
                mutable,
                normalized_policies,
                context.type_rules,
                context.assembly_open,
            )

    async def assign_packet(
        self,
        tournament_id: UUID,
        packet_id: UUID,
        manager_id: UUID,
        *,
        adopted_version_id: UUID | None = None,
        discoverable: bool = False,
        playable: bool = False,
        content_visible: bool = False,
        editable: bool = False,
        library_viewing_rule: str | None = None,
    ) -> UUID:
        if library_viewing_rule is not None and library_viewing_rule not in LIBRARY_VIEWING_RULES:
            raise ValueError(f"Unknown library viewing rule: {library_viewing_rule}")
        async with self.database.transaction() as session:
            await self._require_manager(session, tournament_id, manager_id)
            await self.require_modifiable(session, tournament_id)
            context = await self.context(session, tournament_id)
            if await session.get(LogicalPacketRecord, packet_id) is None:
                raise LookupError("Packet not found")
            if adopted_version_id is not None:
                adopted = await session.get(PacketVersionRecord, adopted_version_id)
                if (
                    adopted is None
                    or adopted.packet_id != packet_id
                    or adopted.state != "published"
                ):
                    raise ValueError("Adopted version must be a published version of the packet")
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == packet_id,
                )
            )
            values = {
                "adopted_version_id": adopted_version_id,
                "discoverable_by_members": discoverable,
                "playable_by_members": playable,
                "content_visible_by_members": content_visible,
                "editable_by_members": editable,
                "library_viewing_rule": library_viewing_rule or context.policies.get(
                    "library_viewing_rule_default", "after-play"
                ),
                "assigned_by_id": manager_id,
                "status": "active",
            }
            if assignment is None:
                assignment = TournamentPacketAssignmentRecord(
                    tournament_id=tournament_id, packet_id=packet_id, **values
                )
                session.add(assignment)
                await session.flush()
                await SubscriptionService.apply_to_new_assignment(
                    session, assignment, context.type_key
                )
            else:
                for key, value in values.items():
                    setattr(assignment, key, value)
            version_id = adopted_version_id or await session.scalar(
                select(PacketVersionRecord.id)
                .where(
                    PacketVersionRecord.packet_id == packet_id,
                    PacketVersionRecord.state == "published",
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
            if version_id is not None:
                # Every manager of this tournament can now read the assigned packet.
                await burn_author_content(
                    session,
                    version_id=version_id,
                    player_ids=await tournament_manager_ids(session, (tournament_id,)),
                )
            await self._invalidate_assembling_lobbies(session, tournament_id)
            return assignment.id

    async def set_packet_entitlement(
        self,
        assignment_id: UUID,
        player_id: UUID,
        manager_id: UUID,
        **rights: bool,
    ) -> None:
        unknown = set(rights) - PACKET_RIGHTS
        if unknown:
            raise ValueError(f"Unknown packet entitlement: {sorted(unknown)[0]}")
        if any(not isinstance(value, bool) for value in rights.values()):
            raise ValueError("Packet rights must be boolean")
        async with self.database.transaction() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            if assignment is None:
                raise LookupError("Tournament packet assignment not found")
            await self._require_manager(session, assignment.tournament_id, manager_id)
            await self.require_modifiable(session, assignment.tournament_id)
            entitlement = await session.get(
                TournamentPacketEntitlementRecord, (assignment_id, player_id)
            )
            if entitlement is None:
                entitlement = TournamentPacketEntitlementRecord(
                    assignment_id=assignment_id,
                    player_id=player_id,
                    granted_by_id=manager_id,
                )
                session.add(entitlement)
            for right, granted in rights.items():
                setattr(entitlement, right, granted)
                override = {
                    "discoverable": "discoverable_override",
                    "content_visible": "readable_override",
                }.get(right)
                if override:
                    setattr(entitlement, override, granted)
            entitlement.revoked_at = None
            await self._invalidate_assembling_lobbies(session, assignment.tournament_id)

    async def require_packet_access(
        self, tournament_id: UUID, packet_id: UUID, player_id: UUID, right: str
    ) -> TournamentPacketAssignmentRecord:
        if right not in PACKET_RIGHTS:
            raise ValueError(f"Unknown packet entitlement: {right}")
        async with self.database.sessions() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == packet_id,
                    TournamentPacketAssignmentRecord.status == "active",
                )
            )
            if assignment is None:
                raise LookupError("Packet is not assigned to this tournament")
            if not await self.has_assignment_access(session, assignment, player_id, right):
                raise PermissionError(f"Packet {right} entitlement is required")
            return assignment

    async def can_read_packet_library(
        self,
        tournament_id: UUID,
        packet_id: UUID,
        player_id: UUID,
        *,
        version_id: UUID | None = None,
    ) -> bool:
        """Compose the release gate and read eligibility for the adopted version."""
        async with self.database.sessions() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == packet_id,
                    TournamentPacketAssignmentRecord.status == "active",
                )
            )
            if assignment is None:
                return False
            version = await session.scalar(
                select(PacketVersionRecord)
                .where(
                    PacketVersionRecord.packet_id == packet_id,
                    PacketVersionRecord.state == "published",
                    *(
                        [PacketVersionRecord.id == assignment.adopted_version_id]
                        if assignment.adopted_version_id
                        else []
                    ),
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
            if (
                version is None
                or version_id is not None
                and version.id != version_id
            ):
                return False
            return await self.can_read_assignment(session, assignment, version, player_id)

    @classmethod
    async def can_read_assignment(
        cls,
        session: AsyncSession,
        assignment: TournamentPacketAssignmentRecord,
        version: PacketVersionRecord,
        player_id: UUID,
    ) -> bool:
        if assignment.status != "active" or version.state != "published":
            return False
        tournament = await session.get(TournamentRecord, assignment.tournament_id)
        if tournament is None or tournament.moderation_status == "abolished":
            return False
        if await cls._is_manager(session, assignment.tournament_id, player_id):
            return True
        if version.library_released_at is None or not await cls.has_assignment_access(
            session, assignment, player_id, "content_visible"
        ):
            return False
        rule = await cls.library_viewing_rule(session, assignment, player_id)
        if rule == "anytime":
            return True
        if rule != "after-play":
            return False
        return bool(
            await session.scalar(
                select(PlayerExposureClaimRecord.id)
                .join(
                    GameParticipantRecord,
                    and_(
                        GameParticipantRecord.game_id == PlayerExposureClaimRecord.game_id,
                        GameParticipantRecord.player_id == player_id,
                    )
                )
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == PlayerExposureClaimRecord.packet_version_id,
                )
                .where(
                    PlayerExposureClaimRecord.player_id == player_id,
                    PlayerExposureClaimRecord.state == "burnt",
                    PacketVersionRecord.packet_id == assignment.packet_id,
                    GameRecord.tournament_id == assignment.tournament_id,
                )
                .limit(1)
            )
        )

    @classmethod
    async def has_assignment_access(
        cls,
        session: AsyncSession,
        assignment: TournamentPacketAssignmentRecord,
        player_id: UUID,
        right: str,
    ) -> bool:
        if right not in PACKET_RIGHTS:
            raise ValueError(f"Unknown packet entitlement: {right}")
        if assignment.status != "active":
            return False
        tournament = await session.get(TournamentRecord, assignment.tournament_id)
        if tournament is None or tournament.moderation_status == "abolished":
            return False
        if right in {"playable", "editable"} and tournament.moderation_status == "halted":
            return False
        if await cls._is_manager(session, assignment.tournament_id, player_id):
            return True
        membership = await session.get(
            TournamentMembershipRecord, (assignment.tournament_id, player_id)
        )
        if membership is None or membership.status != "active":
            return False
        if right in {"playable", "discoverable"}:
            type_key = await session.scalar(
                select(TournamentTypeVersionRecord.key)
                .join(
                    TournamentRecord,
                    TournamentRecord.type_version_id == TournamentTypeVersionRecord.id,
                )
                .where(TournamentRecord.id == assignment.tournament_id)
            )
            if type_key == "classic":
                return await ClassicService.assignment_access(
                    session, assignment.id, player_id, right
                )
        entitlement = await session.get(
            TournamentPacketEntitlementRecord, (assignment.id, player_id)
        )
        override = {
            "discoverable": "discoverable_override", "content_visible": "readable_override",
        }.get(right)
        if entitlement is not None and entitlement.revoked_at is None and override:
            explicit = getattr(entitlement, override)
            if explicit is not None:
                return explicit
        if (
            right == "playable"
            and entitlement is not None
            and entitlement.revoked_at is None
            and entitlement.playable is not None
        ):
            return entitlement.playable
        return bool(
            getattr(assignment, f"{right}_by_members")
            or (
                entitlement is not None
                and entitlement.revoked_at is None
                and getattr(entitlement, right)
            )
        )

    @classmethod
    async def library_viewing_rule(
        cls,
        session: AsyncSession,
        assignment: TournamentPacketAssignmentRecord,
        player_id: UUID,
    ) -> str:
        if assignment.status != "active":
            return "never"
        if await cls._is_manager(session, assignment.tournament_id, player_id):
            return "anytime"
        membership = await session.get(
            TournamentMembershipRecord, (assignment.tournament_id, player_id)
        )
        if membership is None or membership.status != "active":
            return "never"
        return assignment.library_viewing_rule

    async def effective_parameters(
        self, tournament_id: UUID, overrides: dict[str, object] | None = None
    ) -> RulesetParameters:
        async with self.database.sessions() as session:
            context = await self.context(session, tournament_id)
            changes = overrides or {}
            locked = set(changes) - context.mutable_parameters
            if locked:
                raise PermissionError(f"Tournament locks parameter: {sorted(locked)[0]}")
            return context.settings.updated(changes)

    async def context(
        self, session: AsyncSession, tournament_id: UUID, *, lock: bool = False
    ) -> TournamentContext:
        query = select(TournamentRecord).where(TournamentRecord.id == tournament_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        tournament = await session.scalar(query)
        if tournament is None:
            raise LookupError("Tournament not found")
        type_version = await session.get(TournamentTypeVersionRecord, tournament.type_version_id)
        ruleset_version = await session.get(
            GameRulesetVersionRecord, tournament.game_ruleset_version_id
        )
        policy = await session.scalar(
            select(TournamentPolicyVersionRecord)
            .where(TournamentPolicyVersionRecord.tournament_id == tournament.id)
            .order_by(TournamentPolicyVersionRecord.version.desc())
            .limit(1)
        )
        assert type_version is not None and ruleset_version is not None and policy is not None
        ruleset = self.rulesets.get(ruleset_version.key, ruleset_version.version)
        assembly_open = bool(
            tournament.status == "active"
            and tournament.moderation_status == "normal"
            and tournament.finalized_at is not None
            and tournament.actual_starts_at is not None
            and (
                type_version.rules.get("open_ended") is True
                or tournament.participants_finalized_at is not None
            )
        )
        parameters, mutable = tournament_parameters(
            type_version.key, ruleset_version.key,
            policy.default_parameters, policy.player_mutable_parameters,
        )
        resolved = dict(parameters)
        if resolved.get("theme_count") == MAX_THEME_COUNT:
            resolved["theme_count"] = 128
        return TournamentContext(
            tournament.id,
            type_version.id,
            type_version.key,
            ruleset_version.id,
            ruleset_version.key,
            ruleset_version.version,
            policy.id,
            policy.version,
            ruleset.parameters(resolved),
            mutable,
            dict(policy.policies),
            dict(type_version.rules),
            assembly_open,
        )

    async def _invalidate_assembling_lobbies(
        self, session: AsyncSession, tournament_id: UUID
    ) -> None:
        lobbies = tuple(
            (
                await session.execute(
                    select(PregameLobbyRecord)
                    .where(
                        PregameLobbyRecord.tournament_id == tournament_id,
                        PregameLobbyRecord.status == "assembling",
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        now = datetime.now(UTC)
        context = await self.context(session, tournament_id) if lobbies else None
        for lobby in lobbies:
            if context and lobby.tournament_policy_version_id != context.policy_version_id:
                previous = await session.get(
                    TournamentPolicyVersionRecord, lobby.tournament_policy_version_id
                )
                current = await session.get(
                    TournamentPolicyVersionRecord, context.policy_version_id
                )
                overrides = {
                    key: value for key, value in lobby.settings.items()
                    if key in context.mutable_parameters
                    and previous.default_parameters.get(key) != value
                }
                maximum_themes = lobby.settings.get("theme_count") == MAX_THEME_COUNT and (
                    overrides.get("theme_count") == MAX_THEME_COUNT
                    or (
                        current is not None
                        and current.default_parameters.get("theme_count") == MAX_THEME_COUNT
                    )
                )
                effective = context.settings.updated(
                    {
                        key: value
                        for key, value in overrides.items()
                        if not (key == "theme_count" and value == MAX_THEME_COUNT)
                    }
                )
                lobby.settings = effective.to_dict()
                if maximum_themes:
                    lobby.settings = {**lobby.settings, "theme_count": MAX_THEME_COUNT}
                lobby.tournament_policy_version_id = context.policy_version_id
                if context.type_key != "classic":
                    lobby.max_players = int(lobby.settings["maximum_players"])
                lobby.searching = False
                lobby.search_started_at = None
            lobby.validation = []
            lobby.version += 1
            lobby.updated_at = now
            members = (
                await session.execute(
                    select(PregameLobbyMemberRecord).where(
                        PregameLobbyMemberRecord.lobby_id == lobby.id,
                        PregameLobbyMemberRecord.active.is_(True),
                    )
                )
            ).scalars()
            for member in members:
                member.ready = False
                member.validation_violations = []
            sequence = (
                await session.scalar(
                    select(func.max(PregameLobbyEventRecord.sequence)).where(
                        PregameLobbyEventRecord.lobby_id == lobby.id
                    )
                )
                or 0
            )
            session.add(
                PregameLobbyEventRecord(
                    lobby_id=lobby.id,
                    sequence=sequence + 1,
                    kind="lobby_context_invalidated",
                    payload={},
                )
            )
            await TransactionalOutbox.enqueue_lobby_event(
                session,
                lobby_id=lobby.id,
                sequence=sequence + 1,
                kind="lobby_context_invalidated",
                parameters={},
            )

    async def _visible_listing_rows(
        self,
        session: AsyncSession,
        *,
        player_id: UUID | None,
        role: str,
        tournament_id: UUID | None = None,
        include_managed_public: bool = False,
    ) -> list[tuple[object, ...]]:
        if player_id is None:
            if role != "player":
                raise PermissionError("Authentication is required for this role")
        else:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
        if role == "admin":
            await self._require_administrator(session, player_id)
        query = (
            self._listing_query()
            .add_columns(TournamentMembershipRecord, TournamentManagerRecord)
            .outerjoin(
                TournamentMembershipRecord,
                and_(
                    TournamentMembershipRecord.tournament_id == TournamentRecord.id,
                    TournamentMembershipRecord.player_id == player_id,
                ),
            )
            .outerjoin(
                TournamentManagerRecord,
                and_(
                    TournamentManagerRecord.tournament_id == TournamentRecord.id,
                    TournamentManagerRecord.player_id == player_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                ),
            )
        )
        if tournament_id is not None:
            query = query.where(TournamentRecord.id == tournament_id)
        if role == "player":
            query = query.where(
                TournamentRecord.status != "draft",
                TournamentRecord.finalized_at.is_not(None),
                or_(
                    TournamentRecord.visibility == "public",
                    TournamentMembershipRecord.player_id.is_not(None),
                ),
            )
            if not include_managed_public:
                query = query.where(TournamentManagerRecord.player_id.is_(None))
        elif role == "manager":
            query = query.where(TournamentManagerRecord.player_id.is_not(None))
        return list((await session.execute(query)).all())

    async def _visible_list_item(
        self,
        session: AsyncSession,
        row: tuple[object, ...],
        now: datetime,
        role: str,
    ) -> TournamentListItem:
        tournament, type_version, ruleset_version, policy, membership, manager = row
        phase = self._phase(tournament, now)
        registration_open = self._registration_is_open(tournament, now)
        membership_status = membership.status if membership is not None else None
        managed = manager is not None
        actions = ["info"]
        if role in {"manager", "player"} and managed:
            actions.append("select_manager")
        elif role == "player":
            if membership_status == "active":
                actions.append("select_player")
            elif (
                registration_open
                and phase != "past"
                and tournament.status == "active"
                and membership_status not in {"registered", "approved"}
                and (
                    tournament.visibility == "public"
                    or (
                        membership is not None
                        and (
                            membership.status == "invited" or membership.enrolled_by_id is not None
                        )
                    )
                )
            ):
                actions.append("register")
        return TournamentListItem(
            id=tournament.id,
            name=tournament.name,
            slug=tournament.slug,
            status=tournament.status,
            visibility=tournament.visibility,
            starts_at=tournament.starts_at,
            planned_ends_at=tournament.planned_ends_at,
            actual_ends_at=tournament.actual_ends_at,
            actual_starts_at=tournament.actual_starts_at,
            language=tournament.language,
            payment_type=tournament.payment_type,
            pricing_plans=await self.pricing_plans(session, tournament.id),
            registration_open=registration_open,
            registration_starts_at=tournament.registration_starts_at,
            registration_ends_at=tournament.registration_ends_at,
            authors=await self.tournament_authors(session, tournament.id),
            type_key=type_version.key,
            type_version=type_version.version,
            ruleset_key=ruleset_version.key,
            ruleset_version=ruleset_version.version,
            joinable="register" in actions,
            membership_status=membership_status,
            phase=phase,
            managed=managed,
            policy_version=policy.version,
            available_actions=tuple(actions),
            finalized_at=tournament.finalized_at,
            settings_version=tournament.settings_version,
            description=tournament.description,
        )

    @staticmethod
    def _normalize_listing_options(
        *,
        role: str = "player",
        phase: str | None = None,
        relationship: str | None = None,
        registration: str | None = None,
        type_key: str | None = None,
        ruleset_key: str | None = None,
        language: str | None = None,
        search: str = "",
        order: str = "starts_asc",
        limit: int = 20,
    ) -> dict[str, object]:
        normalized_role = role.strip().casefold()
        if normalized_role not in {"player", "manager", "admin"}:
            raise ValueError("Tournament listing role is invalid")
        normalized_phase = phase.strip().casefold() if phase else None
        if normalized_phase == "upcoming":
            normalized_phase = "future"
        if normalized_phase not in {None, "future", "ongoing", "past"}:
            raise ValueError("Tournament phase filter is invalid")
        normalized_relationship = relationship.strip().casefold() if relationship else None
        if normalized_relationship not in {
            None,
            "discoverable",
            "registered",
            "approved",
            "participating",
            "managed",
        }:
            raise ValueError("Tournament relationship filter is invalid")
        normalized_registration = registration.strip().casefold() if registration else None
        if normalized_registration not in {None, "open", "closed"}:
            raise ValueError("Tournament registration filter is invalid")
        normalized_order = order.strip().casefold()
        if normalized_order not in {"starts_asc", "starts_desc", "name_asc", "name_desc"}:
            raise ValueError("Tournament ordering is invalid")
        normalized_search = " ".join(search.strip().split()).casefold()
        if len(normalized_search) > 200:
            raise ValueError("Tournament search supports at most 200 characters")
        if not 1 <= limit <= 100:
            raise ValueError("Tournament page size must be between 1 and 100")
        return {
            "role": normalized_role,
            "phase": normalized_phase,
            "relationship": normalized_relationship,
            "registration": normalized_registration,
            "type_key": type_key.strip().casefold() if type_key else None,
            "ruleset_key": ruleset_key.strip().casefold() if ruleset_key else None,
            "language": language.strip().casefold() if language else None,
            "search": normalized_search,
            "order": normalized_order,
            "limit": limit,
        }

    @classmethod
    def _filter_visible_rows(
        cls,
        rows: list[tuple[object, ...]],
        options: dict[str, object],
        now: datetime,
    ) -> list[tuple[object, ...]]:
        relationship_statuses = {
            "registered": {"registered"},
            "approved": {"approved"},
            "participating": {"active"},
        }
        result: list[tuple[object, ...]] = []
        for row in rows:
            tournament, type_version, ruleset_version, _, membership, manager = row
            if options["phase"] is not None and cls._phase(tournament, now) != options["phase"]:
                continue
            relationship = options["relationship"]
            if relationship == "discoverable" and (
                tournament.visibility != "public" or tournament.status == "draft"
            ):
                continue
            if relationship == "managed" and manager is None:
                continue
            if relationship in relationship_statuses and (
                membership is None or membership.status not in relationship_statuses[relationship]
            ):
                continue
            registration_open = cls._registration_is_open(tournament, now)
            if options["registration"] == "open" and not registration_open:
                continue
            if options["registration"] == "closed" and registration_open:
                continue
            if (
                options["type_key"] is not None
                and type_version.key.casefold() != options["type_key"]
            ):
                continue
            if (
                options["ruleset_key"] is not None
                and ruleset_version.key.casefold() != options["ruleset_key"]
            ):
                continue
            if (
                options["language"] is not None
                and tournament.language.casefold() != options["language"]
            ):
                continue
            query = options["search"]
            if (
                query
                and query not in tournament.name.casefold()
                and query not in tournament.slug.casefold()
            ):
                continue
            result.append(row)
        return result

    @staticmethod
    def _sort_visible_rows(rows: list[tuple[object, ...]], order: object) -> None:
        if order in {"name_asc", "name_desc"}:
            rows.sort(key=lambda row: (row[0].name.casefold(), row[0].slug, row[0].id))
            if order == "name_desc":
                rows.reverse()
            return
        minimum = datetime.min.replace(tzinfo=UTC)
        maximum = datetime.max.replace(tzinfo=UTC)
        if order == "starts_desc":
            rows.sort(
                key=lambda row: (
                    row[0].starts_at or minimum,
                    row[0].name.casefold(),
                    row[0].id,
                ),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda row: (
                    row[0].starts_at or maximum,
                    row[0].name.casefold(),
                    row[0].id,
                )
            )

    @staticmethod
    def _filter_visible_items(
        items: list[TournamentListItem], options: dict[str, object]
    ) -> list[TournamentListItem]:
        relationship_statuses = {
            "registered": {"registered"},
            "approved": {"approved"},
            "participating": {"active"},
        }
        result: list[TournamentListItem] = []
        for item in items:
            if options["phase"] is not None and item.phase != options["phase"]:
                continue
            relationship = options["relationship"]
            if relationship == "discoverable" and (
                item.visibility != "public" or item.status == "draft"
            ):
                continue
            if relationship == "managed" and not item.managed:
                continue
            if (
                relationship in relationship_statuses
                and item.membership_status not in relationship_statuses[relationship]
            ):
                continue
            if options["registration"] == "open" and not item.registration_open:
                continue
            if options["registration"] == "closed" and item.registration_open:
                continue
            if options["type_key"] is not None and item.type_key.casefold() != options["type_key"]:
                continue
            if (
                options["ruleset_key"] is not None
                and item.ruleset_key.casefold() != options["ruleset_key"]
            ):
                continue
            if options["language"] is not None and item.language.casefold() != options["language"]:
                continue
            query = options["search"]
            if query and query not in item.name.casefold() and query not in item.slug.casefold():
                continue
            result.append(item)
        return result

    @staticmethod
    def _sort_visible_items(items: list[TournamentListItem], order: object) -> None:
        if order in {"name_asc", "name_desc"}:
            items.sort(key=lambda item: (item.name.casefold(), item.slug, item.id))
            if order == "name_desc":
                items.reverse()
            return
        minimum = datetime.min.replace(tzinfo=UTC)
        maximum = datetime.max.replace(tzinfo=UTC)
        if order == "starts_desc":
            items.sort(
                key=lambda item: (item.starts_at or minimum, item.name.casefold(), item.id),
                reverse=True,
            )
        else:
            items.sort(key=lambda item: (item.starts_at or maximum, item.name.casefold(), item.id))

    @staticmethod
    def _listing_signature(options: dict[str, object]) -> str:
        payload = {key: value for key, value in options.items() if key != "limit"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    @staticmethod
    def _encode_listing_cursor(offset: int, signature: str) -> str:
        payload = json.dumps({"offset": offset, "query": signature}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_listing_cursor(cursor: str | None, signature: str) -> int:
        if cursor is None:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode())
            offset = payload["offset"]
            if (
                not isinstance(offset, int)
                or isinstance(offset, bool)
                or offset < 0
                or payload["query"] != signature
            ):
                raise ValueError
            return offset
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid tournament listing cursor") from error

    @staticmethod
    def _listing_query(*, include_membership: bool = False):
        latest_policy_versions = (
            select(
                TournamentPolicyVersionRecord.tournament_id.label("tournament_id"),
                func.max(TournamentPolicyVersionRecord.version).label("version"),
            )
            .group_by(TournamentPolicyVersionRecord.tournament_id)
            .subquery()
        )
        entities: list[object] = [
            TournamentRecord,
            TournamentTypeVersionRecord,
            GameRulesetVersionRecord,
            TournamentPolicyVersionRecord,
        ]
        if include_membership:
            entities.append(TournamentMembershipRecord)
        query = (
            select(*entities)
            .join(
                TournamentTypeVersionRecord,
                TournamentTypeVersionRecord.id == TournamentRecord.type_version_id,
            )
            .join(
                GameRulesetVersionRecord,
                GameRulesetVersionRecord.id == TournamentRecord.game_ruleset_version_id,
            )
            .join(
                latest_policy_versions,
                latest_policy_versions.c.tournament_id == TournamentRecord.id,
            )
            .join(
                TournamentPolicyVersionRecord,
                (TournamentPolicyVersionRecord.tournament_id == TournamentRecord.id)
                & (TournamentPolicyVersionRecord.version == latest_policy_versions.c.version),
            )
        )
        if include_membership:
            query = query.join(
                TournamentMembershipRecord,
                TournamentMembershipRecord.tournament_id == TournamentRecord.id,
            )
        return query

    async def _group_listing(
        cls,
        session: AsyncSession,
        rows: list[tuple[object, ...]],
        now: datetime,
        *,
        include_membership: bool = False,
    ) -> TournamentListing:
        groups: dict[str, list[TournamentListItem]] = {
            "ongoing": [],
            "future": [],
            "past": [],
        }
        for row in rows:
            tournament, type_version, ruleset_version, policy = row[:4]
            membership = row[4] if include_membership else None
            phase = cls._phase(tournament, now)
            groups[phase].append(
                TournamentListItem(
                    id=tournament.id,
                    name=tournament.name,
                    slug=tournament.slug,
                    status=tournament.status,
                    visibility=tournament.visibility,
                    starts_at=tournament.starts_at,
                    planned_ends_at=tournament.planned_ends_at,
                    actual_ends_at=tournament.actual_ends_at,
                    actual_starts_at=tournament.actual_starts_at,
                    language=tournament.language,
                    payment_type=tournament.payment_type,
                    pricing_plans=await cls.pricing_plans(session, tournament.id),
                    registration_open=cls._registration_is_open(tournament, now),
                    registration_starts_at=tournament.registration_starts_at,
                    registration_ends_at=tournament.registration_ends_at,
                    authors=await cls.tournament_authors(session, tournament.id),
                    type_key=type_version.key,
                    type_version=type_version.version,
                    ruleset_key=ruleset_version.key,
                    ruleset_version=ruleset_version.version,
                    joinable=(
                        phase != "past"
                        and tournament.status == "active"
                        and cls._registration_is_open(tournament, now)
                    ),
                    membership_status=membership.status if membership is not None else None,
                    phase=phase,
                    policy_version=policy.version,
                    finalized_at=tournament.finalized_at,
                    settings_version=tournament.settings_version,
                    description=tournament.description,
                )
            )
        groups["ongoing"].sort(
            key=lambda item: (
                item.starts_at or datetime.min.replace(tzinfo=UTC),
                item.name.casefold(),
                item.id,
            )
        )
        groups["future"].sort(
            key=lambda item: (
                item.starts_at or datetime.max.replace(tzinfo=UTC),
                item.name.casefold(),
                item.id,
            )
        )
        groups["past"].sort(
            key=lambda item: (
                item.actual_ends_at
                or item.planned_ends_at
                or item.starts_at
                or datetime.min.replace(tzinfo=UTC),
                item.name.casefold(),
                item.id,
            ),
            reverse=True,
        )
        return TournamentListing(
            ongoing=tuple(groups["ongoing"]),
            future=tuple(groups["future"]),
            past=tuple(groups["past"]),
        )

    @staticmethod
    def _phase(tournament: TournamentRecord, now: datetime) -> str:
        if tournament.status in {"completed", "archived"}:
            return "past"
        if tournament.actual_ends_at is not None:
            return "past"
        return "ongoing" if tournament.actual_starts_at is not None else "future"

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Tournament listing time must include a timezone")
        return value

    @staticmethod
    def _validate_schedule(
        registration_starts_at: datetime | None,
        registration_ends_at: datetime | None,
        starts_at: datetime | None,
        planned_ends_at: datetime | None,
    ) -> None:
        for field, value in (
            ("registration_starts_at", registration_starts_at),
            ("registration_ends_at", registration_ends_at),
            ("starts_at", starts_at),
            ("planned_ends_at", planned_ends_at),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field} must include a timezone")
        if (
            registration_starts_at is not None
            and registration_ends_at is not None
            and registration_ends_at <= registration_starts_at
        ):
            raise ValueError("Registration end must be after registration start")
        if (
            starts_at is not None
            and registration_ends_at is not None
            and starts_at < registration_ends_at
        ):
            raise ValueError("Tournament start cannot precede registration end")
        if starts_at is not None and planned_ends_at is not None and planned_ends_at <= starts_at:
            raise ValueError("Planned tournament finish must be after its start")

    async def _evaluate_registration_requirements(
        self, session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> tuple[
        tuple[TournamentRegistrationRequirementRecord, ...],
        tuple[str, ...],
    ]:
        requirements = tuple(
            (
                await session.execute(
                    select(TournamentRegistrationRequirementRecord)
                    .where(
                        TournamentRegistrationRequirementRecord.tournament_id == tournament_id,
                        TournamentRegistrationRequirementRecord.revoked_at.is_(None),
                    )
                    .order_by(TournamentRegistrationRequirementRecord.created_at)
                )
            ).scalars()
        )
        failures: list[str] = []
        for requirement in requirements:
            failed = False
            if requirement.kind in {
                "has-played-tournament",
                "has-not-played-tournament",
            }:
                assert requirement.target_tournament_id is not None
                played = await self._has_played_tournament(
                    session, player_id, requirement.target_tournament_id
                )
                failed = not played if requirement.kind == "has-played-tournament" else played
            elif requirement.kind == "has-not-seen-packet":
                assert requirement.target_packet_id is not None
                failed = await self._has_seen_packet(
                    session, player_id, requirement.target_packet_id
                )
            if failed:
                snapshot = await self._requirement_snapshot(session, requirement)
                failures.append(
                    requirement.failure_message or self._default_requirement_failure(snapshot)
                )
        return requirements, tuple(failures)

    @staticmethod
    async def _has_played_tournament(
        session: AsyncSession, player_id: UUID, tournament_id: UUID
    ) -> bool:
        return (
            await session.scalar(
                select(GameParticipantRecord.id)
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .join(
                    PlayerExposureClaimRecord,
                    and_(
                        PlayerExposureClaimRecord.game_id == GameParticipantRecord.game_id,
                        PlayerExposureClaimRecord.player_id == GameParticipantRecord.player_id,
                    ),
                )
                .where(
                    GameParticipantRecord.player_id == player_id,
                    GameRecord.tournament_id == tournament_id,
                    PlayerExposureClaimRecord.state == "burnt",
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    async def _has_seen_packet(session: AsyncSession, player_id: UUID, packet_id: UUID) -> bool:
        return (
            await session.scalar(
                select(PlayerExposureClaimRecord.id)
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == PlayerExposureClaimRecord.packet_version_id,
                )
                .where(
                    PlayerExposureClaimRecord.player_id == player_id,
                    PlayerExposureClaimRecord.state == "burnt",
                    PacketVersionRecord.packet_id == packet_id,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    async def _requirement_snapshot(
        session: AsyncSession,
        requirement: TournamentRegistrationRequirementRecord,
    ) -> RegistrationRequirementSnapshot:
        if requirement.target_tournament_id is not None:
            target = await session.get(TournamentRecord, requirement.target_tournament_id)
            if target is None:
                raise LookupError("Requirement target tournament not found")
            target_id = target.id
            target_name = target.name
        else:
            assert requirement.target_packet_id is not None
            version = await session.scalar(
                select(PacketVersionRecord)
                .where(
                    PacketVersionRecord.packet_id == requirement.target_packet_id,
                    PacketVersionRecord.state == "published",
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
            target_id = requirement.target_packet_id
            target_name = version.name if version is not None else str(target_id)
        return RegistrationRequirementSnapshot(
            id=requirement.id,
            kind=requirement.kind,
            target_id=target_id,
            target_name=target_name,
            failure_message=requirement.failure_message,
        )

    @staticmethod
    def _default_requirement_failure(requirement: RegistrationRequirementSnapshot) -> str:
        if requirement.kind == "has-played-tournament":
            return f"Prior participation in tournament '{requirement.target_name}' is required."
        if requirement.kind == "has-not-played-tournament":
            return f"Prior participation in tournament '{requirement.target_name}' is not allowed."
        return f"Packet '{requirement.target_name}' has already been seen."

    @staticmethod
    def _scheduled_registration_is_open(tournament: TournamentRecord, now: datetime) -> bool:
        return bool(
            tournament.status == "active"
            and tournament.finalized_at is not None
            and tournament.registration_open
            and (
                tournament.registration_starts_at is None
                or tournament.registration_starts_at <= now
            )
            and (
                not tournament.ignore_late_registrations
                or tournament.registration_ends_at is None
                or now < tournament.registration_ends_at
            )
        )

    @classmethod
    def _registration_is_open(cls, tournament: TournamentRecord, now: datetime) -> bool:
        if tournament.status != "active" or tournament.finalized_at is None:
            return False
        override = getattr(tournament, "registration_open_override", None)
        if override is not None:
            return bool(override)
        return cls._scheduled_registration_is_open(tournament, now)

    @staticmethod
    def _pricing(
        payment_type: str, pricing_plans: object
    ) -> tuple[str, tuple[tuple[str, tuple[PricingPlanPriceSnapshot, ...]], ...]]:
        normalized_type = payment_type.strip().casefold().replace("_", "-")
        if normalized_type not in PAYMENT_TYPES:
            raise ValueError("Payment type must be free, one-time, or per-stage")
        if not isinstance(pricing_plans, list | tuple):
            raise ValueError("pricing_plans must be a list")
        normalized_plans: list[tuple[str, tuple[PricingPlanPriceSnapshot, ...]]] = []
        seen_names: set[str] = set()
        for raw_plan in pricing_plans:
            if not isinstance(raw_plan, dict):
                raise ValueError("Each pricing plan must be an object")
            name_value = raw_plan.get("name")
            if not isinstance(name_value, str) or not name_value.strip():
                raise ValueError("Each pricing plan requires a name")
            name = " ".join(name_value.split())
            if len(name) > 100:
                raise ValueError("Pricing plan names support at most 100 characters")
            name_key = name.casefold()
            if name_key in seen_names:
                raise ValueError("Pricing plan names must be unique")
            seen_names.add(name_key)
            raw_prices = raw_plan.get("prices")
            if not isinstance(raw_prices, list | tuple) or not raw_prices:
                raise ValueError("Each pricing plan requires at least one price")
            prices: list[PricingPlanPriceSnapshot] = []
            seen_currencies: set[str] = set()
            for raw_price in raw_prices:
                if not isinstance(raw_price, dict):
                    raise ValueError("Each pricing plan price must be an object")
                currency_value = raw_price.get("currency")
                currency = currency_value.strip().upper() if isinstance(currency_value, str) else ""
                if not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError("Prices require a three-letter ISO currency code")
                if currency in seen_currencies:
                    raise ValueError("A pricing plan may define each currency only once")
                seen_currencies.add(currency)
                try:
                    amount = Decimal(str(raw_price.get("amount")))
                except Exception as error:
                    raise ValueError("Pricing plan amounts must be numbers") from error
                if not amount.is_finite() or amount <= 0:
                    raise ValueError("Pricing plan amounts must be finite and positive")
                if amount.as_tuple().exponent < -2:
                    raise ValueError("Pricing plan amounts support at most two decimals")
                prices.append(PricingPlanPriceSnapshot(amount=amount, currency=currency))
            normalized_plans.append((name, tuple(prices)))
        if normalized_type == "free" and normalized_plans:
            raise ValueError("A free tournament cannot have pricing plans")
        if normalized_type != "free" and not normalized_plans:
            raise ValueError("A paid tournament requires at least one pricing plan")
        return normalized_type, tuple(normalized_plans)

    @staticmethod
    async def _create_author(session: AsyncSession, name: str) -> AuthorRecord:
        author = AuthorRecord(display_name=name)
        session.add(author)
        await session.flush()
        return author

    @staticmethod
    def _normalize_author_component(value: str, label: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized or len(normalized) > 100:
            raise ValueError(f"{label} must contain at most 100 characters")
        return normalized

    @staticmethod
    def _normalize_telegram_link(value: str | None) -> tuple[str | None, str | None]:
        if value is None or not value.strip():
            return None, None
        normalized = value.strip()
        match = re.fullmatch(
            r"(?:(?:https?://)?(?:www\.)?t\.me/|@)?([A-Za-z0-9_]{1,64})/?",
            normalized,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise ValueError("Telegram link must identify a Telegram username")
        username = match.group(1)
        return f"https://t.me/{username}", username

    @staticmethod
    def _manager_policy_descriptors(
        policies: dict[str, object],
    ) -> tuple[ManagerSettingDescriptor, ...]:
        appeal = AppealPolicy.from_mapping(policies)
        effective: dict[str, object] = {
            "library_viewing_rule_default": policies.get(
                "library_viewing_rule_default", "after-play"
            ),
            **{
                name: policies.get(name, default)
                for name, default in PACKET_ACCESS_DEFAULT_POLICIES.items()
            },
            "hybrid_matchmaking_enabled": policies.get("hybrid_matchmaking_enabled", False),
            AUTO_APPROVE_REGISTRATIONS_POLICY: policies.get(
                AUTO_APPROVE_REGISTRATIONS_POLICY, False,
            ),
            MEMBER_UPLOADS_POLICY: policies.get(MEMBER_UPLOADS_POLICY, False),
            PACKET_NOTIFICATIONS_POLICY: policies.get(PACKET_NOTIFICATIONS_POLICY, True),
            "observing": policies.get("observing", "forbidden"),
            "maximum_participants": policies.get("maximum_participants"),
            PACKETS_PER_LOBBY_POLICY: policies.get(PACKETS_PER_LOBBY_POLICY, "one"),
            "appeal_voting_rule": policies.get("appeal_voting_rule", appeal.voting_rule),
            "appeal_vote_timeout_seconds": policies.get(
                "appeal_vote_timeout_seconds", appeal.vote_timeout_seconds
            ),
            "appeal_escalation_enabled": policies.get(
                "appeal_escalation_enabled", appeal.escalation_enabled
            ),
            "appeal_escalation_decision_timeout_seconds": policies.get(
                "appeal_escalation_decision_timeout_seconds",
                appeal.escalation_decision_timeout_seconds,
            ),
            "appeal_commentary_timeout_seconds": policies.get(
                "appeal_commentary_timeout_seconds", appeal.commentary_timeout_seconds
            ),
            "appeal_ticket_expiry_seconds": policies.get(
                "appeal_ticket_expiry_seconds", appeal.ticket_expiry_seconds
            ),
        }
        for key, value in policies.items():
            if key not in {RULESET_RATING_WEIGHT_POLICY}:
                effective.setdefault(key, value)
        enum_options = {
            "library_viewing_rule_default": (
                "never", "after-play", "anytime"
            ),
            "observing": ("unlimited", "burnt-only", "forbidden"),
            "appeal_voting_rule": ("majority", "unanimous"),
            PACKETS_PER_LOBBY_POLICY: ("one", "any"),
        }
        descriptors = []
        for name, value in effective.items():
            value_type = (
                "enum"
                if name in enum_options
                else "boolean"
                if isinstance(value, bool)
                else "integer"
                if isinstance(value, int) or value is None
                else "number"
                if isinstance(value, float)
                else "string"
            )
            descriptors.append(
                ManagerSettingDescriptor(
                    name,
                    value_type,
                    f"policy.{name}.description",
                    value,
                    enum_options.get(name, ()),
                )
            )
        return tuple(descriptors)

    @staticmethod
    def _normalize_author_name(name: str) -> str:
        normalized = " ".join(name.strip().split())
        if not normalized:
            raise ValueError("Author name cannot be empty")
        if len(normalized) > 300:
            raise ValueError("Author name cannot exceed 300 characters")
        return normalized

    @staticmethod
    def _snapshot(
        tournament: TournamentRecord,
        type_version: TournamentTypeVersionRecord,
        ruleset_version: GameRulesetVersionRecord,
        policy_version: int,
        pricing_plans: tuple[PricingPlanSnapshot, ...],
    ) -> TournamentSnapshot:
        return TournamentSnapshot(
            id=tournament.id,
            name=tournament.name,
            slug=tournament.slug,
            status=tournament.status,
            type_key=type_version.key,
            type_version=type_version.version,
            ruleset_key=ruleset_version.key,
            ruleset_version=ruleset_version.version,
            policy_version=policy_version,
            visibility=tournament.visibility,
            starts_at=tournament.starts_at,
            planned_ends_at=tournament.planned_ends_at,
            actual_ends_at=tournament.actual_ends_at,
            actual_starts_at=tournament.actual_starts_at,
            language=tournament.language,
            payment_type=tournament.payment_type,
            pricing_plans=pricing_plans,
            registration_open=tournament.registration_open,
            registration_starts_at=tournament.registration_starts_at,
            registration_ends_at=tournament.registration_ends_at,
            finalized_at=tournament.finalized_at,
            settings_version=tournament.settings_version,
        )

    @staticmethod
    def _token_digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @staticmethod
    async def _latest_type(session: AsyncSession, key: str) -> TournamentTypeVersionRecord:
        record = await session.scalar(
            select(TournamentTypeVersionRecord)
            .where(TournamentTypeVersionRecord.key == key, TournamentTypeVersionRecord.active)
            .order_by(TournamentTypeVersionRecord.version.desc())
            .limit(1)
        )
        if record is None:
            raise LookupError(f"Active tournament type not found: {key}")
        return record

    @staticmethod
    async def _latest_ruleset(session: AsyncSession, key: str) -> GameRulesetVersionRecord:
        record = await session.scalar(
            select(GameRulesetVersionRecord)
            .where(GameRulesetVersionRecord.key == key, GameRulesetVersionRecord.active)
            .order_by(GameRulesetVersionRecord.version.desc())
            .limit(1)
        )
        if record is None:
            raise LookupError(f"Active game ruleset not found: {key}")
        return record

    @staticmethod
    async def _active_tournament(session: AsyncSession, tournament_id: UUID) -> TournamentRecord:
        tournament = await session.get(TournamentRecord, tournament_id)
        if tournament is None or tournament.status != "active":
            raise LookupError("Active tournament not found")
        return tournament

    @staticmethod
    async def _require_administrator(session: AsyncSession, player_id: UUID) -> None:
        administrator = await session.get(PlatformAdministratorRecord, player_id)
        if administrator is None or administrator.revoked_at is not None:
            raise PermissionError("Platform administrator role is required")

    @staticmethod
    async def _is_manager(session: AsyncSession, tournament_id: UUID, player_id: UUID) -> bool:
        manager = await session.get(TournamentManagerRecord, (tournament_id, player_id))
        return manager is not None and manager.revoked_at is None

    @staticmethod
    async def _has_active_participation(
        session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> bool:
        lobby_player = await session.scalar(
            select(PregameLobbyMemberRecord.player_id)
            .join(PregameLobbyRecord, PregameLobbyRecord.id == PregameLobbyMemberRecord.lobby_id)
            .where(
                PregameLobbyRecord.tournament_id == tournament_id,
                PregameLobbyRecord.status == "assembling",
                PregameLobbyMemberRecord.player_id == player_id,
                PregameLobbyMemberRecord.active.is_(True),
                PregameLobbyMemberRecord.role == "player",
            )
            .limit(1)
        )
        if lobby_player is not None:
            return True
        game_player = await session.scalar(
            select(GameParticipantRecord.player_id)
            .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
            .where(
                GameRecord.tournament_id == tournament_id,
                GameRecord.status.in_(("lobby", "active")),
                GameParticipantRecord.player_id == player_id,
            )
            .limit(1)
        )
        return game_player is not None

    @classmethod
    async def _require_manager(
        cls, session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> None:
        if not await cls._is_manager(session, tournament_id, player_id):
            raise PermissionError("Tournament manager role is required")

    @staticmethod
    async def require_modifiable(session: AsyncSession, tournament_id: UUID) -> TournamentRecord:
        tournament = await session.scalar(
            select(TournamentRecord).where(TournamentRecord.id == tournament_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if tournament is None:
            raise LookupError("Tournament not found")
        if tournament.moderation_status != "normal":
            raise PermissionError("Tournament is halted or abolished")
        return tournament
