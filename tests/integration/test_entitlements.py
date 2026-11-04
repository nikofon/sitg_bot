import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import create_player, seed_category
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.services.ai_administration import AIAdministrationService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.entitlements import (
    FEATURE_DISSECTION,
    RESET_MONTHLY,
    TRAINING_FEATURES,
    EntitlementService,
    FeatureDisabledError,
    QuotaExceededError,
)
from sitg_bot.services.training_ai import ProviderSettings
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerRecord,
    TrainingAnalysisArtifactRecord,
    TrainingCategoryAssignmentRecord,
    TrainingFeaturePolicyRecord,
    TrainingPolicyStateRecord,
    TrainingUsageLedgerRecord,
    TrainingUsagePeriodRecord,
)

pytestmark = pytest.mark.integration


def payload(**per_feature):
    base = {
        feature: {"enabled": True, "limit": None, "reset_period": "none"}
        for feature in TRAINING_FEATURES
    }
    for feature, overrides in per_feature.items():
        base[feature] = base[feature] | overrides
    return base


async def ai_admin_setup(database):
    actor = await create_player(database, 92001, "AI-admin")
    player = await create_player(database, 92002, "AI-player")
    async with database.transaction() as session:
        session.add(PlatformAdministratorRecord(player_id=actor))
    await EntitlementService(database).ensure_defaults()
    service = AIAdministrationService(database, settings=ProviderSettings(
        "openai", "fixture-model", api_key="fixture-private-key",
    ))
    return service, actor, player


def ai_settings(version=1, **overrides):
    return {
        "expected_version": version,
        "dissection_enabled": True, "best_buzz_enabled": True,
        "free": {"dissection": 2, "best_buzz": 3},
        "premium": {"dissection": None, "best_buzz": 10},
        "reset_period": "day", **overrides,
    }


async def test_ai_admin_denies_nonadministrators_at_every_service_entry(database):
    service, actor, player = await ai_admin_setup(database)
    calls = (
        (service.read, {}), (service.update, ai_settings()),
        (service.reset, {"confirm": True, "expected_version": 1}),
        (service.players, {"search": "AI"}),
        (service.tier, {"player_id": actor, "tier": "premium"}),
        (service.health, {}),
    )
    for method, values in calls:
        with pytest.raises(PermissionError, match="administrator"):
            await method(player, **values)
    management = AdminManagementService(database)
    with pytest.raises(PermissionError):
        await management.catalogue(player, "ai")
    assert await management.catalogue(actor, "ai") == {
        "kind": "admin_management", "state": "ready", "section": "ai", "items": [],
    }
    async with database.transaction() as session:
        admin = await session.get(PlatformAdministratorRecord, actor)
        admin.revoked_at = datetime.now(UTC)
    with pytest.raises(PermissionError):
        await service.read(actor)


async def test_ai_admin_defaults_explicit_settings_and_version_fencing(database):
    service, actor, player = await ai_admin_setup(database)
    initial = await service.read(actor)
    assert initial["version"] == 1 and initial["quota_generation"] == 0
    assert initial["free"] == initial["premium"] == {"dissection": 24, "best_buzz": 24}
    assert not initial["dissection_enabled"] and not initial["best_buzz_enabled"]
    before = await service.entitlements.capabilities(player)
    assert not before["dissection"].enabled
    result = await service.update(actor, **ai_settings(reset_period="week"))
    assert result["version"] == 2 and result["reset_period"] == "week"
    capabilities = await service.entitlements.capabilities(player)
    assert capabilities["dissection"].remaining == 2
    assert capabilities["best_buzz"].remaining == 3
    assert not capabilities["common_knowledge"].enabled
    assert capabilities["dissection"].reset_at.weekday() == 0
    with pytest.raises(StaleWriteError):
        await service.update(actor, **ai_settings())
    with pytest.raises(StaleWriteError):
        await service.reset(actor, confirm=True, expected_version=1)
    await service.entitlements.ensure_defaults(features=payload())
    assert (await service.read(actor))["version"] == 2
    async with database.transaction() as session:
        first = await session.scalar(select(TrainingFeaturePolicyRecord).where(
            TrainingFeaturePolicyRecord.version == 1
        ))
        assert first.features["dissection"]["enabled"] is False


async def test_ai_manual_tiers_unlimited_zero_no_ai_and_shared_budget(database):
    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    await reserve(database, player, "tier-one")
    await reserve(database, player, "tier-two")
    with pytest.raises(QuotaExceededError):
        await reserve(database, player, "tier-three")
    await service.tier(actor, player_id=player, tier="premium")
    assert (await service.entitlements.capabilities(player))["dissection"].remaining is None
    await reserve(database, player, "premium-one")
    await service.tier(actor, player_id=player, tier="free")
    assert (await service.entitlements.capabilities(player))["dissection"].remaining == 0
    await service.tier(actor, player_id=player, tier="no_ai")
    for feature in TRAINING_FEATURES:
        async with database.transaction() as session:
            with pytest.raises(FeatureDisabledError):
                await service.entitlements.authorize(session, player_id=player,
                    feature=feature, request_id="cache-" + feature, charge=False)
    # Already accepted work survives downgrades and settles/refunds once.
    async with database.transaction() as session:
        assert await service.entitlements.settle(session, player_id=player,
            feature="dissection", request_id="premium-one")
    await service.reset(actor, confirm=True, expected_version=2)
    async with database.transaction() as session:
        assert await service.entitlements.release(session, player_id=player,
            feature="dissection", request_id="tier-one")
        assert not await service.entitlements.release(session, player_id=player,
            feature="dissection", request_id="tier-one")
    status = await service.entitlements.capabilities(player)
    assert all(not item.enabled for item in status.values())
    await service.tier(actor, player_id=player, tier="free")
    await service.update(actor, **ai_settings(version=3,
        free={"dissection": 0, "best_buzz": 3}))
    with pytest.raises(QuotaExceededError):
        await reserve(database, player, "zero")
    async with database.transaction() as session:
        # Existing free-cache charging convention is retained, but authorization applies.
        assert await service.entitlements.authorize(session, player_id=player,
            feature="dissection", request_id="free-cache", charge=False) is None


@pytest.mark.parametrize("tier", ["free", "premium", "no_ai"])
async def test_first_tier_assignment_does_not_deadlock_player_locked_best_buzz(
    database, monkeypatch, tier,
):
    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    reached_lock = asyncio.Event()
    original_get = AsyncSession.get
    assignment = None

    async def get(session, model, key, **kwargs):
        assigning = asyncio.current_task() is assignment
        if assigning and model is PlayerRecord and kwargs.get("with_for_update"):
            reached_lock.set()
        result = await original_get(session, model, key, **kwargs)
        if assigning and model is TrainingPolicyStateRecord and kwargs.get("with_for_update"):
            reached_lock.set()
        return result

    monkeypatch.setattr(AsyncSession, "get", get)
    try:
        async with asyncio.timeout(5):
            async with database.transaction() as session:
                await session.get(PlayerRecord, player, with_for_update=True)
                assignment = asyncio.create_task(service.tier(actor, player_id=player, tier=tier))
                await reached_lock.wait()
                policy = await service.entitlements.policy_for(session, player, "best_buzz")
                assert policy.enabled and policy.limit == 3
            assert await assignment == {"id": player, "tier": tier}
    finally:
        if assignment is not None:
            assignment.cancel()
            await asyncio.gather(assignment, return_exceptions=True)
    status = await service.entitlements.capabilities(player)
    assert status["best_buzz"].enabled is (tier != "no_ai")


@pytest.mark.parametrize("tier", ["free", "premium", "no_ai"])
async def test_tier_assignment_does_not_deadlock_policy_locked_quota_admission(
    database, monkeypatch, tier,
):
    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    reached_policy = asyncio.Event()
    original_state = service._state

    async def state(session, actor, *, lock=False):
        if lock:
            reached_policy.set()
        return await original_state(session, actor, lock=lock)

    monkeypatch.setattr(service, "_state", state)
    assignment = None
    try:
        async with asyncio.timeout(5):
            async with database.transaction() as session:
                await service.entitlements.policy_for(session, player, "dissection")
                assignment = asyncio.create_task(service.tier(actor, player_id=player, tier=tier))
                await reached_policy.wait()
                reservation = await service.entitlements.authorize(
                    session, player_id=player, feature="dissection", request_id="admitted",
                )
                assert reservation is not None
            assert await assignment == {"id": player, "tier": tier}
    finally:
        if assignment is not None:
            assignment.cancel()
            await asyncio.gather(assignment, return_exceptions=True)
    async with database.transaction() as session:
        assert await service.entitlements.settle(
            session, player_id=player, feature="dissection", request_id="admitted",
        )


async def test_ai_reset_preserves_inflight_and_identities_and_all_features(database):
    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    # Custom category enables common knowledge without changing its existing quota.
    await seed_category(database, "reset-custom", payload(), [player])
    reservations = []
    for feature in TRAINING_FEATURES:
        async with database.transaction() as session:
            reservations.append(await service.entitlements.authorize(session,
                player_id=player, feature=feature, request_id="old-" + feature))
    reset = await service.reset(actor, confirm=True, expected_version=2)
    assert reset["quota_generation"] == 1
    for feature in TRAINING_FEATURES:
        async with database.transaction() as session:
            new = await service.entitlements.authorize(session, player_id=player,
                feature=feature, request_id="new-" + feature)
            assert new.period_id not in {r.period_id for r in reservations}
            assert await service.entitlements.settle(session, player_id=player,
                feature=feature, request_id="old-" + feature)
            assert not await service.entitlements.settle(session, player_id=player,
                feature=feature, request_id="old-" + feature)
    async with database.transaction() as session:
        count = await session.scalar(select(func.count()).select_from(TrainingUsageLedgerRecord))
        assert count == 9
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord)))
        assert len(periods) == 6
        assert all(p.reserved == 0 and p.settled == 1 for p in periods if p.quota_generation == 0)
        assert all(p.reserved == 1 and p.settled == 0 for p in periods if p.quota_generation == 1)
        for method in (service.entitlements.settle, service.entitlements.release):
            with pytest.raises(ValueError, match="another player"):
                await method(session, player_id=actor, feature="dissection",
                             request_id="new-dissection")
        with pytest.raises(ValueError, match="another player"):
            await service.entitlements.authorize(session, player_id=actor,
                feature="dissection", request_id="new-dissection")


async def test_ai_window_switches_do_not_alias_midnight_buckets_or_erase_old_usage(database):
    service, actor, player = await ai_admin_setup(database)
    moment = datetime(2030, 6, 17, 0, 30, tzinfo=UTC)
    await service.update(actor, **ai_settings(reset_period="hour"))
    first = await reserve(database, player, "hour-window", now=moment)
    await service.update(actor, **ai_settings(version=2, reset_period="day"))
    second = await reserve(database, player, "day-window", now=moment)
    assert first.period_id != second.period_id
    await service.update(actor, **ai_settings(version=3, reset_period="hour"))
    status = await service.entitlements.capabilities(player, now=moment)
    assert status["dissection"].remaining == 1
    assert status["dissection"].reset_at == datetime(2030, 6, 17, 1, tzinfo=UTC)


async def test_ai_migration_roundtrip_preserves_active_reservations(database):
    from alembic import command
    from alembic.config import Config

    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    await reserve(database, player, "before-downgrade")
    await service.reset(actor, confirm=True, expected_version=2)
    await reserve(database, player, "after-generation")
    await database.close()
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0030_public_analysis_cache")
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    async with database.transaction() as session:
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord)))
        assert len(periods) == 1 and periods[0].reserved == 2
        for key in ("before-downgrade", "after-generation"):
            assert await service.entitlements.settle(session, player_id=player,
                feature="dissection", request_id=key)
        assert periods[0].reserved == 0 and periods[0].settled == 2


async def test_no_ai_denies_prepared_best_buzz_cache_without_touching_source(database):
    from sitg_bot.services.training_knowledge import KnowledgeApplication

    service, actor, player = await ai_admin_setup(database)
    await service.update(actor, **ai_settings())
    await service.tier(actor, player_id=player, tier="no_ai")
    app = KnowledgeApplication(database, settings=service.settings, analysis=object())
    async with database.transaction() as session:
        assert await app.prepared(session, player, None) is None


async def test_ai_custom_categories_and_roles_survive_global_toggles(database):
    service, actor, player = await ai_admin_setup(database)
    custom_id = await seed_category(database, "intentional-off", payload(
        dissection={"enabled": False, "limit": 7, "cache_hit_charging": True,
                    "queued_request_cap": 1},
        best_buzz={"limit": 8, "in_flight_request_cap": 1},
        common_knowledge={"limit": 9},
    ), [player])
    initial = await service.entitlements.capabilities(player)
    assert not initial["dissection"].enabled and initial["dissection"].limit == 24
    assert not initial["best_buzz"].enabled and initial["best_buzz"].limit == 24
    await service.update(actor, **ai_settings())
    status = await service.entitlements.capabilities(player)
    assert status["dissection"].enabled and status["dissection"].limit == 2
    assert status["best_buzz"].enabled and status["best_buzz"].limit == 3
    assert status["dissection"].cache_hit_charging
    assert status["dissection"].queued_request_cap == 1
    assert status["best_buzz"].in_flight_request_cap == 1
    assert status["common_knowledge"].enabled and status["common_knowledge"].limit == 9
    await reserve(database, player, "custom-free")
    await service.tier(actor, player_id=player, tier="free")
    explicit = await service.entitlements.capabilities(player)
    assert explicit["dissection"].remaining == 1
    assert explicit["dissection"].cache_hit_charging
    assert explicit["best_buzz"] == status["best_buzz"]
    async with database.transaction() as session:
        assert await service.entitlements.settle(session, player_id=player,
            feature="dissection", request_id="custom-free")
    await service.update(actor, **ai_settings(version=2, best_buzz_enabled=False))
    assert not (await service.entitlements.capabilities(player))["best_buzz"].enabled
    await service.tier(actor, player_id=player, tier="premium")
    async with database.transaction() as session:
        assignment = await session.get(TrainingCategoryAssignmentRecord, player)
        assert assignment.category_id == custom_id
        assert await session.get(PlatformAdministratorRecord, actor) is not None


async def test_ai_player_search_is_limited_literal_and_projects_only_manual_tier(database):
    service, actor, player = await ai_admin_setup(database)
    assert (await service.players(actor, search=str(player)))["items"] == [
        {"id": player, "name": "AI-player", "tier": "free"}
    ]
    assert len((await service.players(actor, search="ai", limit=1))["items"]) == 1
    assert not (await service.players(actor, search="AI%"))["items"]
    assert not (await service.players(actor, search="player"))["items"]
    await service.tier(actor, player_id=player, tier="no_ai")
    assert (await service.players(actor, search="AI-player"))["items"][0]["tier"] == "no_ai"
    with pytest.raises(ValueError):
        await service.players(actor, search="a", limit=26)
    with pytest.raises(LookupError):
        await service.tier(actor, player_id=uuid4(), tier="premium")


async def test_ai_health_uses_only_successful_matching_profile_recorded_calls(database):
    service, actor, player = await ai_admin_setup(database)
    observed = datetime.now(UTC) - timedelta(minutes=1)
    async with database.transaction() as session:
        for index, profile, calls, completed in (
            (1, "other-profile", [{"request_id": "other"}], datetime.now(UTC)),
            (2, service.settings.execution_profile, [], datetime.now(UTC)),
            (3, service.settings.execution_profile, [{"request_id": "recorded"}], observed),
        ):
            session.add(TrainingAnalysisArtifactRecord(
                artifact_key=str(index) * 64, kind="dissection", owner_player_id=player,
                source_fingerprint="source", instruction_digest="instruction", language="en",
                model=service.settings.model, prompt_version="2", schema_version="2",
                tokenizer_version="announcement-v1", status="succeeded", payload={},
                completed_at=completed,
                provider_usage={"execution_profile": profile, "calls": calls},
            ))
    result = await service.health(actor)
    assert result["state"] == "responding" and result["verified_at"] == observed
    assert "fixture-private-key" not in str(result)
    async with database.transaction() as session:
        artifact = await session.scalar(select(TrainingAnalysisArtifactRecord).where(
            TrainingAnalysisArtifactRecord.artifact_key == "3" * 64
        ))
        artifact.completed_at = datetime.now(UTC) - timedelta(minutes=6)
    assert (await service.health(actor))["state"] == "stale"
    other = AIAdministrationService(database, settings=ProviderSettings(
        "charm", "fixture-model", api_key="fixture-key"))
    assert (await other.health(actor))["state"] == "unknown"
    service.settings = ProviderSettings("openai", "unconfigured")
    assert (await service.health(actor))["state"] == "unconfigured"


async def reserve(database, player_id, request_id, *, now=None):
    entitlements = EntitlementService(database)
    async with database.transaction() as session:
        return await entitlements.authorize(
            session,
            player_id=player_id,
            feature=FEATURE_DISSECTION,
            request_id=request_id,
            now=now,
        )


async def test_concurrent_reservations_cannot_overspend(database):
    player_id = await create_player(database, 101, "spender")
    await seed_category(database, "limited", payload(dissection={"limit": 3}), [player_id])
    results = await asyncio.gather(
        *(reserve(database, player_id, f"req-{index}") for index in range(10)),
        return_exceptions=True,
    )
    succeeded = [item for item in results if not isinstance(item, BaseException)]
    quota_errors = [item for item in results if isinstance(item, QuotaExceededError)]
    assert len(succeeded) == 3
    assert len(quota_errors) == 7
    capabilities = await EntitlementService(database).capabilities(player_id)
    assert capabilities[FEATURE_DISSECTION].remaining == 0


async def test_duplicate_request_ids_settle_once_and_release_refunds(database):
    player_id = await create_player(database, 102, "idempotent")
    await seed_category(database, "bounded", payload(dissection={"limit": 5}), [player_id])
    entitlements = EntitlementService(database)

    first = await reserve(database, player_id, "same-key")
    second = await reserve(database, player_id, "same-key")
    assert first is not None and second is not None
    assert first.reservation_id == second.reservation_id

    async with database.transaction() as session:
        assert await entitlements.settle(
            session, player_id=player_id, feature=FEATURE_DISSECTION, request_id="same-key"
        )
        assert not await entitlements.settle(
            session, player_id=player_id, feature=FEATURE_DISSECTION, request_id="same-key"
        )
    assert (await entitlements.capabilities(player_id))[FEATURE_DISSECTION].remaining == 4

    failed_id = f"fail-{uuid4()}"
    await reserve(database, player_id, failed_id)
    async with database.transaction() as session:
        assert await entitlements.release(
            session, player_id=player_id, feature=FEATURE_DISSECTION, request_id=failed_id
        )
        assert not await entitlements.release(
            session, player_id=player_id, feature=FEATURE_DISSECTION, request_id=failed_id
        )
    assert (await entitlements.capabilities(player_id))[FEATURE_DISSECTION].remaining == 4


async def test_partial_settlement_records_provider_usage(database):
    player_id = await create_player(database, 103, "partial")
    await seed_category(database, "partial-cat", payload(dissection={"limit": 5}), [player_id])
    entitlements = EntitlementService(database)
    await reserve(database, player_id, "partial-key")
    async with database.transaction() as session:
        assert await entitlements.settle(
            session,
            player_id=player_id,
            feature=FEATURE_DISSECTION,
            request_id="partial-key",
            used_units=1,
            provider_usage={"provider": "fake", "tokens": 900},
        )
    status = (await entitlements.capabilities(player_id))[FEATURE_DISSECTION]
    assert status.remaining == 4


async def test_period_rollover_resets_quota(database):
    player_id = await create_player(database, 104, "monthly")
    await seed_category(
        database,
        "monthly-cat",
        payload(dissection={"limit": 2, "reset_period": RESET_MONTHLY}),
        [player_id],
    )
    entitlements = EntitlementService(database)
    january = datetime(2030, 1, 15, tzinfo=UTC)
    february = datetime(2030, 2, 3, tzinfo=UTC)

    await reserve(database, player_id, "jan-1", now=january)
    await reserve(database, player_id, "jan-2", now=january)
    with pytest.raises(QuotaExceededError):
        await reserve(database, player_id, "jan-3", now=january)

    reservation = await reserve(database, player_id, "feb-1", now=february)
    assert reservation is not None
    capabilities = await entitlements.capabilities(player_id, now=february)
    assert capabilities[FEATURE_DISSECTION].remaining == 1
    assert capabilities[FEATURE_DISSECTION].reset_at == datetime(2030, 3, 1, tzinfo=UTC)


async def test_category_change_applies_new_limits_immediately(database):
    player_id = await create_player(database, 105, "switcher")
    await seed_category(database, "small", payload(dissection={"limit": 1}), [player_id])
    await reserve(database, player_id, "first")

    await seed_category(database, "large", payload(dissection={"limit": 5}), [player_id])
    entitlements = EntitlementService(database)
    capabilities = await entitlements.capabilities(player_id)
    assert capabilities[FEATURE_DISSECTION].limit == 5
    assert capabilities[FEATURE_DISSECTION].remaining == 4
    assert await reserve(database, player_id, "after-switch") is not None


async def test_disabled_features_raise_stable_errors(database):
    player_id = await create_player(database, 106, "disabled")
    await seed_category(database, "off", payload(dissection={"enabled": False}), [player_id])
    with pytest.raises(FeatureDisabledError):
        await reserve(database, player_id, "never")


async def test_reconcile_abandoned_releases_stale_reservations_only(database):
    player_id = await create_player(database, 107, "crashy")
    await seed_category(database, "crash-cat", payload(dissection={"limit": 4}), [player_id])
    entitlements = EntitlementService(database)
    stale_moment = datetime(2030, 1, 1, tzinfo=UTC)
    later = stale_moment + timedelta(hours=2)

    await reserve(database, player_id, "stale", now=stale_moment)
    await reserve(database, player_id, "fresh", now=later)

    released = await entitlements.reconcile_abandoned(older_than=timedelta(hours=1), now=later)
    assert released == 1
    capabilities = await entitlements.capabilities(player_id, now=later)
    assert capabilities[FEATURE_DISSECTION].remaining == 3

    released_again = await entitlements.reconcile_abandoned(
        older_than=timedelta(hours=1), now=later
    )
    assert released_again == 0

