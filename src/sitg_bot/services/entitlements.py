"""Trainee feature categories, entitlement policies and metered usage.

Agent C module (see `docs/training-mode-plan.md`). Categories are separate
from tournament roles and subscriptions. Limits and reset rules are
configurable through versioned feature policies; billing and plan purchase
flows are deferred. Reservation, settlement and release are server-side,
atomic and idempotent per request ID.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    Base,
    TrainingAnalysisRequestRecord,
    TrainingCategoryAssignmentRecord,
    TrainingCategoryRecord,
    TrainingFeaturePolicyRecord,
    TrainingPolicyStateRecord,
    TrainingUsageLedgerRecord,
    TrainingUsagePeriodRecord,
)

FEATURE_DISSECTION = "dissection"
FEATURE_COMMON_KNOWLEDGE = "common_knowledge"
FEATURE_BEST_BUZZ = "best_buzz"
TRAINING_FEATURES = (FEATURE_DISSECTION, FEATURE_COMMON_KNOWLEDGE, FEATURE_BEST_BUZZ)

RESET_NONE = "none"
RESET_DAILY = "daily"
RESET_MONTHLY = "monthly"
RESET_PERIODS = (RESET_NONE, RESET_DAILY, RESET_MONTHLY)

LEDGER_RESERVE = "reserve"
LEDGER_SETTLE = "settle"
LEDGER_RELEASE = "release"

#: Used when a player has no explicit category assignment.
DEFAULT_CATEGORY_CODE = "default"


ENTITLEMENT_METADATA = Base.metadata
EntitlementBase = Base


class EntitlementsError(Exception):
    """Entitlement failure with a stable, adapter-facing error code."""

    code = "entitlements_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FeatureDisabledError(EntitlementsError):
    code = "feature_disabled"


class QuotaExceededError(EntitlementsError):
    code = "quota_exceeded"


@dataclass(frozen=True, slots=True)
class FeaturePolicy:
    """Validated per-feature policy: limits, resets and charging behavior."""

    enabled: bool = False
    limit: int | None = None
    reset_period: str = RESET_NONE
    cache_hit_charging: bool = True
    queued_request_cap: int | None = None
    in_flight_request_cap: int | None = None

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ValueError("Feature limit must be at least 1 when bounded")
        if self.reset_period not in RESET_PERIODS:
            raise ValueError(f"Unknown reset period {self.reset_period!r}")
        for cap in (self.queued_request_cap, self.in_flight_request_cap):
            if cap is not None and cap < 1:
                raise ValueError("Request caps must be at least 1 when bounded")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FeaturePolicy":
        unknown = set(payload) - {
            "enabled",
            "limit",
            "reset_period",
            "cache_hit_charging",
            "queued_request_cap",
            "in_flight_request_cap",
        }
        if unknown:
            raise ValueError(f"Unknown feature policy keys: {sorted(unknown)}")
        return cls(
            enabled=bool(payload.get("enabled", False)),
            limit=payload.get("limit"),
            reset_period=str(payload.get("reset_period", RESET_NONE)),
            cache_hit_charging=bool(payload.get("cache_hit_charging", True)),
            queued_request_cap=payload.get("queued_request_cap"),
            in_flight_request_cap=payload.get("in_flight_request_cap"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "limit": self.limit,
            "reset_period": self.reset_period,
            "cache_hit_charging": self.cache_hit_charging,
            "queued_request_cap": self.queued_request_cap,
            "in_flight_request_cap": self.in_flight_request_cap,
        }


def parse_feature_policies(payload: Mapping[str, Any]) -> dict[str, FeaturePolicy]:
    """Validate a policy payload; every known feature must be configured."""
    if set(payload) != set(TRAINING_FEATURES):
        raise ValueError("Feature policy must configure exactly the known features")
    return {feature: FeaturePolicy.from_mapping(payload[feature]) for feature in TRAINING_FEATURES}


#: Basic free allowances; provider configuration controls feature availability.
DEFAULT_POLICY_PAYLOAD: dict[str, Any] = {
    feature: {
        "enabled": False,
        "limit": 50 if feature == FEATURE_COMMON_KNOWLEDGE else 24,
        "reset_period": RESET_DAILY,
        "cache_hit_charging": False,
        "queued_request_cap": 3,
        "in_flight_request_cap": 2,
    }
    for feature in TRAINING_FEATURES
}

UNBOUNDED_PERIOD_START = datetime(1970, 1, 1, tzinfo=UTC)
UNBOUNDED_PERIOD_END = datetime(9999, 12, 31, tzinfo=UTC)


def period_bounds(reset_period: str, now: datetime) -> tuple[datetime, datetime]:
    """Return the [start, end) bounds of the usage period containing ``now``."""
    if reset_period == RESET_NONE:
        return UNBOUNDED_PERIOD_START, UNBOUNDED_PERIOD_END
    moment = now.astimezone(UTC)
    if reset_period == RESET_DAILY:
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    """Adapter-facing projection of one feature's entitlement state."""

    feature: str
    enabled: bool
    allowed: bool
    limit: int | None
    remaining: int | None
    reset_at: datetime | None
    cache_hit_charging: bool
    queued_request_cap: int | None
    in_flight_request_cap: int | None
    unit: str = "themes"


@dataclass(frozen=True, slots=True)
class Reservation:
    """A granted quota reservation, settled or released exactly once."""

    reservation_id: UUID
    period_id: UUID
    player_id: UUID
    feature: str
    request_id: str
    units: int


class EntitlementService:
    """Server-authoritative category entitlements and quota metering."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def ensure_defaults(self, *, features: Mapping[str, Any] | None = None) -> None:
        """Create the default disabled policy, category and state idempotently."""
        async with self.database.transaction() as session:
            policy_row = (
                (
                    await session.execute(
                        select(TrainingFeaturePolicyRecord).order_by(
                            TrainingFeaturePolicyRecord.version
                        )
                    )
                )
                .scalars()
                .first()
            )
            if policy_row is None:
                policy_row = TrainingFeaturePolicyRecord(
                    version=1, features=dict(features or DEFAULT_POLICY_PAYLOAD)
                )
                session.add(policy_row)
                await session.flush()
            state = await session.get(TrainingPolicyStateRecord, 1)
            if state is None:
                category = TrainingCategoryRecord(
                    code=DEFAULT_CATEGORY_CODE,
                    display_name="Basic free",
                    is_default=True,
                    policy_version=policy_row.version,
                )
                session.add(category)
                await session.flush()
                session.add(
                    TrainingPolicyStateRecord(singleton=1, default_category_id=category.id)
                )

    async def create_policy(self, session: AsyncSession, payload: Mapping[str, Any]) -> int:
        """Validate and append a new immutable policy version."""
        policies = parse_feature_policies(payload)
        latest = await session.scalar(select(func.max(TrainingFeaturePolicyRecord.version)))
        version = (latest or 0) + 1
        session.add(
            TrainingFeaturePolicyRecord(
                version=version,
                features={feature: policy.to_mapping() for feature, policy in policies.items()},
            )
        )
        return version

    async def create_category(
        self,
        session: AsyncSession,
        *,
        code: str,
        display_name: str,
        policy_version: int,
        is_default: bool = False,
        now: datetime | None = None,
    ) -> UUID:
        """Create a category bound to an existing policy version."""
        if not code or not display_name:
            raise ValueError("Category code and display name are required")
        exists_version = await session.scalar(
            select(TrainingFeaturePolicyRecord.id).where(
                TrainingFeaturePolicyRecord.version == policy_version
            )
        )
        if exists_version is None:
            raise ValueError(f"Policy version {policy_version} does not exist")
        category = TrainingCategoryRecord(
            code=code,
            display_name=display_name,
            is_default=is_default,
            policy_version=policy_version,
        )
        category.created_at = now or datetime.now(UTC)
        session.add(category)
        await session.flush()
        return category.id

    async def assign_category(
        self,
        session: AsyncSession,
        *,
        player_id: UUID,
        category_id: UUID,
        now: datetime | None = None,
    ) -> None:
        """Assign (or replace) the player's category; new limits apply at once."""
        moment = now or datetime.now(UTC)
        existing = await session.get(TrainingCategoryAssignmentRecord, player_id)
        if existing is None:
            session.add(
                TrainingCategoryAssignmentRecord(
                    player_id=player_id, category_id=category_id, assigned_at=moment
                )
            )
        else:
            existing.category_id = category_id
            existing.assigned_at = moment

    async def policy_for(
        self, session: AsyncSession, player_id: UUID, feature: str
    ) -> FeaturePolicy:
        """Return the player's effective policy for one feature."""
        if feature not in TRAINING_FEATURES:
            raise ValueError(f"Unknown training feature {feature!r}")
        policies = await self._effective_policies(session, player_id)
        return policies[feature]

    async def capabilities(
        self, player_id: UUID, *, now: datetime | None = None
    ) -> dict[str, FeatureStatus]:
        """Project allowed features, remaining quota and reset information."""
        moment = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            policies = await self._effective_policies(session, player_id)
            statuses: dict[str, FeatureStatus] = {}
            for feature, policy in policies.items():
                start, end = period_bounds(policy.reset_period, moment)
                period = (
                    await session.execute(
                        select(TrainingUsagePeriodRecord).where(
                            TrainingUsagePeriodRecord.player_id == player_id,
                            TrainingUsagePeriodRecord.feature == feature,
                            TrainingUsagePeriodRecord.period_start == start,
                        )
                    )
                ).scalar_one_or_none()
                settled = period.settled if period is not None else 0
                reserved = period.reserved if period is not None else 0
                remaining = (
                    None
                    if policy.limit is None
                    else max(policy.limit - settled - reserved, 0)
                )
                statuses[feature] = FeatureStatus(
                    feature=feature,
                    enabled=policy.enabled,
                    allowed=policy.enabled and (remaining is None or remaining > 0),
                    limit=policy.limit,
                    remaining=remaining,
                    reset_at=end if policy.reset_period != RESET_NONE else None,
                    cache_hit_charging=policy.cache_hit_charging,
                    queued_request_cap=policy.queued_request_cap,
                    in_flight_request_cap=policy.in_flight_request_cap,
                    unit="estimates" if feature == FEATURE_COMMON_KNOWLEDGE else "themes",
                )
            return statuses

    async def authorize(
        self,
        session: AsyncSession,
        *,
        player_id: UUID,
        feature: str,
        request_id: str,
        units: int = 1,
        charge: bool = True,
        now: datetime | None = None,
    ) -> Reservation | None:
        """Authorize a feature and atomically reserve quota in the caller's transaction.

        Idempotent per request ID: a repeated authorization returns the
        existing reservation instead of charging twice. Feature authorization
        applies even when ``charge`` is false (for example on cache hits).
        """
        if feature not in TRAINING_FEATURES:
            raise ValueError(f"Unknown training feature {feature!r}")
        if not request_id:
            raise ValueError("Reservation request ID is required")
        if units < 1:
            raise ValueError("Reservation units must be positive")
        moment = now or datetime.now(UTC)
        policy = await self.policy_for(session, player_id, feature)
        if not policy.enabled:
            raise FeatureDisabledError(f"Feature {feature} is disabled")
        if not charge:
            return None
        period = await self._locked_period(session, player_id, feature, policy, moment)
        existing = await self._ledger_entry(session, request_id, LEDGER_RESERVE)
        if existing is not None:
            return Reservation(
                existing.reservation_id,
                existing.period_id,
                existing.player_id,
                existing.feature,
                existing.request_id,
                existing.units,
            )
        if policy.limit is not None and period.settled + period.reserved + units > policy.limit:
            raise QuotaExceededError(f"Feature {feature} quota is exhausted for this period")
        period.reserved += units
        reservation_id = uuid4()
        session.add(
            TrainingUsageLedgerRecord(
                reservation_id=reservation_id,
                period_id=period.id,
                player_id=player_id,
                feature=feature,
                request_id=request_id,
                entry_type=LEDGER_RESERVE,
                units=units,
                created_at=moment,
            )
        )
        return Reservation(reservation_id, period.id, player_id, feature, request_id, units)

    async def settle(
        self,
        session: AsyncSession,
        *,
        player_id: UUID,
        feature: str,
        request_id: str,
        used_units: int = 1,
        provider_usage: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Settle a reservation for successful work; idempotent and refunding.

        Partial success settles only ``used_units`` and releases the rest.
        Provider usage and cost are recorded separately from quota units.
        """
        if used_units < 1:
            raise ValueError("Settled units must be positive")
        moment = now or datetime.now(UTC)
        reserve_entry = await self._ledger_entry(session, request_id, LEDGER_RESERVE)
        if reserve_entry is None:
            return False
        period = await session.get(
            TrainingUsagePeriodRecord, reserve_entry.period_id, with_for_update=True
        )
        if period is None:
            return False
        if (
            await self._ledger_entry(session, request_id, LEDGER_SETTLE) is not None
            or await self._ledger_entry(session, request_id, LEDGER_RELEASE) is not None
        ):
            return False
        units = reserve_entry.units
        if used_units > units:
            raise ValueError("Settled units exceed the reservation")
        period.reserved -= units
        period.settled += used_units
        session.add(
            TrainingUsageLedgerRecord(
                reservation_id=reserve_entry.reservation_id,
                period_id=period.id,
                player_id=player_id,
                feature=feature,
                request_id=request_id,
                entry_type=LEDGER_SETTLE,
                units=used_units,
                provider_usage=dict(provider_usage) if provider_usage is not None else None,
                created_at=moment,
            )
        )
        if units > used_units:
            session.add(
                TrainingUsageLedgerRecord(
                    reservation_id=reserve_entry.reservation_id,
                    period_id=period.id,
                    player_id=player_id,
                    feature=feature,
                    request_id=request_id,
                    entry_type=LEDGER_RELEASE,
                    units=units - used_units,
                    created_at=moment,
                )
            )
        return True

    async def release(
        self,
        session: AsyncSession,
        *,
        player_id: UUID,
        feature: str,
        request_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Release a reservation for failed or cancelled work; idempotent."""
        moment = now or datetime.now(UTC)
        reserve_entry = await self._ledger_entry(session, request_id, LEDGER_RESERVE)
        if reserve_entry is None:
            return False
        period = await session.get(
            TrainingUsagePeriodRecord, reserve_entry.period_id, with_for_update=True
        )
        if period is None:
            return False
        if (
            await self._ledger_entry(session, request_id, LEDGER_SETTLE) is not None
            or await self._ledger_entry(session, request_id, LEDGER_RELEASE) is not None
        ):
            return False
        period.reserved -= reserve_entry.units
        session.add(
            TrainingUsageLedgerRecord(
                reservation_id=reserve_entry.reservation_id,
                period_id=period.id,
                player_id=player_id,
                feature=feature,
                request_id=request_id,
                entry_type=LEDGER_RELEASE,
                units=reserve_entry.units,
                created_at=moment,
            )
        )
        return True

    async def reconcile_abandoned(
        self,
        *,
        older_than: timedelta,
        now: datetime | None = None,
        limit: int = 200,
    ) -> int:
        """Release reservations never settled or released before a crash.

        Callers should schedule this periodically; only reservations older
        than the cutoff are touched so in-flight work keeps its quota.
        """
        if older_than <= timedelta(0):
            raise ValueError("Reconciliation cutoff must be positive")
        if limit < 1:
            raise ValueError("Reconciliation batch limit must be positive")
        moment = now or datetime.now(UTC)
        cutoff = moment - older_than
        settle_entry = aliased(TrainingUsageLedgerRecord)
        release_entry = aliased(TrainingUsageLedgerRecord)
        released = 0
        while True:
            async with self.database.transaction() as session:
                batch = list(
                    (
                        await session.execute(
                            select(TrainingUsageLedgerRecord)
                            .where(
                                TrainingUsageLedgerRecord.entry_type == LEDGER_RESERVE,
                                TrainingUsageLedgerRecord.created_at < cutoff,
                                ~select(TrainingAnalysisRequestRecord.id)
                                .where(
                                    TrainingAnalysisRequestRecord.request_id
                                    == TrainingUsageLedgerRecord.request_id,
                                    TrainingAnalysisRequestRecord.status.in_(("queued", "running")),
                                )
                                .exists(),
                                ~select(settle_entry.id)
                                .where(
                                    settle_entry.request_id == TrainingUsageLedgerRecord.request_id,
                                    settle_entry.entry_type == LEDGER_SETTLE,
                                )
                                .exists(),
                                ~select(release_entry.id)
                                .where(
                                    release_entry.request_id
                                    == TrainingUsageLedgerRecord.request_id,
                                    release_entry.entry_type == LEDGER_RELEASE,
                                )
                                .exists(),
                            )
                            .order_by(TrainingUsageLedgerRecord.created_at)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).scalars()
                )
                for entry in batch:
                    done = await self.release(
                        session,
                        player_id=entry.player_id,
                        feature=entry.feature,
                        request_id=entry.request_id,
                        now=moment,
                    )
                    released += int(done)
            if len(batch) < limit:
                break
        return released

    async def _effective_policies(
        self, session: AsyncSession, player_id: UUID
    ) -> dict[str, FeaturePolicy]:
        """Resolve the policies of the player's (or default) category."""
        assignment = await session.get(TrainingCategoryAssignmentRecord, player_id)
        category: TrainingCategoryRecord | None = None
        if assignment is not None:
            category = await session.get(TrainingCategoryRecord, assignment.category_id)
        else:
            state = await session.get(TrainingPolicyStateRecord, 1)
            if state is not None:
                category = await session.get(TrainingCategoryRecord, state.default_category_id)
        if category is None:
            return parse_feature_policies(DEFAULT_POLICY_PAYLOAD)
        policy_row = await session.scalar(
            select(TrainingFeaturePolicyRecord).where(
                TrainingFeaturePolicyRecord.version == category.policy_version
            )
        )
        if policy_row is None:
            return parse_feature_policies(DEFAULT_POLICY_PAYLOAD)
        return parse_feature_policies(policy_row.features)

    async def _locked_period(
        self,
        session: AsyncSession,
        player_id: UUID,
        feature: str,
        policy: FeaturePolicy,
        moment: datetime,
    ) -> TrainingUsagePeriodRecord:
        """Return the current period row, row-locked, creating it if needed."""
        start, end = period_bounds(policy.reset_period, moment)
        record = (
            await session.execute(
                select(TrainingUsagePeriodRecord)
                .where(
                    TrainingUsagePeriodRecord.player_id == player_id,
                    TrainingUsagePeriodRecord.feature == feature,
                    TrainingUsagePeriodRecord.period_start == start,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is not None:
            return record
        await session.execute(
            pg_insert(TrainingUsagePeriodRecord)
            .values(
                id=uuid4(),
                player_id=player_id,
                feature=feature,
                period_start=start,
                period_end=end,
            )
            .on_conflict_do_nothing(index_elements=["player_id", "feature", "period_start"])
        )
        record = (
            await session.execute(
                select(TrainingUsagePeriodRecord)
                .where(
                    TrainingUsagePeriodRecord.player_id == player_id,
                    TrainingUsagePeriodRecord.feature == feature,
                    TrainingUsagePeriodRecord.period_start == start,
                )
                .with_for_update()
            )
        ).scalar_one()
        return record

    @staticmethod
    async def _ledger_entry(
        session: AsyncSession, request_id: str, entry_type: str
    ) -> TrainingUsageLedgerRecord | None:
        return (
            await session.execute(
                select(TrainingUsageLedgerRecord).where(
                    TrainingUsageLedgerRecord.request_id == request_id,
                    TrainingUsageLedgerRecord.entry_type == entry_type,
                )
            )
        ).scalar_one_or_none()
