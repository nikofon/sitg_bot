from datetime import UTC, datetime

import pytest

from sitg_bot.services.entitlements import (
    RESET_DAILY,
    RESET_MONTHLY,
    RESET_NONE,
    TRAINING_FEATURES,
    UNBOUNDED_PERIOD_END,
    UNBOUNDED_PERIOD_START,
    FeaturePolicy,
    parse_feature_policies,
    period_bounds,
)


def payload(**overrides):
    return {
        feature: {"enabled": True, "limit": 5} | overrides.get(feature, {})
        for feature in TRAINING_FEATURES
    }


def test_feature_policy_parses_and_round_trips() -> None:
    policy = FeaturePolicy.from_mapping(
        {
            "enabled": True,
            "limit": 10,
            "reset_period": RESET_MONTHLY,
            "cache_hit_charging": False,
            "queued_request_cap": 4,
            "in_flight_request_cap": 2,
        }
    )
    restored = FeaturePolicy.from_mapping(policy.to_mapping())
    assert restored == policy
    assert policy.enabled and policy.limit == 10 and policy.cache_hit_charging is False


def test_feature_policy_rejects_unknown_keys_and_invalid_values() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        FeaturePolicy.from_mapping({"enabled": True, "surprise": 1})
    with pytest.raises(ValueError):
        FeaturePolicy.from_mapping({"enabled": True, "limit": 0})
    with pytest.raises(ValueError):
        FeaturePolicy.from_mapping({"enabled": True, "reset_period": "weekly"})
    with pytest.raises(ValueError):
        FeaturePolicy.from_mapping({"enabled": True, "queued_request_cap": 0})


def test_parse_feature_policies_requires_exactly_all_features() -> None:
    complete = payload()
    assert set(parse_feature_policies(complete)) == set(TRAINING_FEATURES)

    missing = dict(complete)
    missing.pop(TRAINING_FEATURES[0])
    with pytest.raises(ValueError, match="exactly"):
        parse_feature_policies(missing)
    with pytest.raises(ValueError, match="exactly"):
        parse_feature_policies(complete | {"extra": {"enabled": True}})


def test_period_bounds_cover_none_daily_and_monthly() -> None:
    moment = datetime(2030, 6, 15, 12, 30, tzinfo=UTC)
    assert period_bounds(RESET_NONE, moment) == (UNBOUNDED_PERIOD_START, UNBOUNDED_PERIOD_END)
    start, end = period_bounds(RESET_DAILY, moment)
    assert start == datetime(2030, 6, 15, tzinfo=UTC)
    assert end == datetime(2030, 6, 16, tzinfo=UTC)
    start, end = period_bounds(RESET_MONTHLY, moment)
    assert start == datetime(2030, 6, 1, tzinfo=UTC)
    assert end == datetime(2030, 7, 1, tzinfo=UTC)
    december = datetime(2030, 12, 2, tzinfo=UTC)
    assert period_bounds(RESET_MONTHLY, december)[1] == datetime(2031, 1, 1, tzinfo=UTC)
