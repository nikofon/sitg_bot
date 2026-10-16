import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import create_player, seed_category

from sitg_bot.services.entitlements import (
    FEATURE_DISSECTION,
    RESET_MONTHLY,
    TRAINING_FEATURES,
    EntitlementService,
    FeatureDisabledError,
    QuotaExceededError,
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

