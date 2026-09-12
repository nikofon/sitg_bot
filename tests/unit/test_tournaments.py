from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from sitg_bot.services.tournaments import (
    TournamentListItem,
    TournamentService,
    normalize_tournament_policies,
)


@pytest.mark.parametrize(
    ("name", "default"),
    (
        ("packets_discoverable_by_default", True),
        ("packets_playable_by_default", False),
        ("packets_readable_by_default", False),
        ("packets_released_by_default", False),
    ),
)
def test_packet_access_policy_defaults_and_manager_editors(name: str, default: bool) -> None:
    assert normalize_tournament_policies({}, {})[name] is default
    descriptors = {
        item.name: item for item in TournamentService._manager_policy_descriptors({})
    }
    assert descriptors[name].value is default
    assert descriptors[name].value_type == "boolean"
    for value in (True, False):
        policies = normalize_tournament_policies({}, {name: value})
        assert policies[name] is value
        descriptors = {
            item.name: item for item in TournamentService._manager_policy_descriptors(policies)
        }
        assert descriptors[name].value is value
    for value in (None, "true", "false", 0, 1, [], {}):
        with pytest.raises(ValueError, match=f"{name} must be a boolean"):
            normalize_tournament_policies({}, {name: value})


def _listing_item(
    number: int,
    name: str,
    *,
    phase: str = "ongoing",
    visibility: str = "public",
    membership_status: str | None = None,
    managed: bool = False,
    registration_open: bool = True,
) -> TournamentListItem:
    return TournamentListItem(
        id=UUID(int=number),
        name=name,
        slug=name.casefold().replace(" ", "-"),
        status="active",
        visibility=visibility,
        starts_at=datetime(2026, 9, number, tzinfo=UTC),
        planned_ends_at=None,
        actual_ends_at=None,
        language="en",
        payment_type="free",
        pricing_plans=(),
        registration_open=registration_open,
        registration_starts_at=None,
        registration_ends_at=None,
        authors=(),
        type_key="ladder",
        type_version=1,
        ruleset_key="si",
        ruleset_version=1,
        joinable=registration_open,
        membership_status=membership_status,
        phase=phase,
        managed=managed,
    )


def test_pricing_plans_are_normalized_and_require_currency() -> None:
    payment_type, plans = TournamentService._pricing(
        "one_time",
        [
            {
                "name": " Students ",
                "prices": [
                    {"amount": "10", "currency": "usd"},
                    {"amount": "9.00", "currency": "eur"},
                ],
            }
        ],
    )
    assert payment_type == "one-time"
    assert plans[0][0] == "Students"
    assert [(price.amount, price.currency) for price in plans[0][1]] == [
        (Decimal("10"), "USD"),
        (Decimal("9.00"), "EUR"),
    ]
    assert TournamentService._pricing("free", []) == ("free", ())

    with pytest.raises(ValueError, match="currency"):
        TournamentService._pricing(
            "per-stage",
            [{"name": "Adults", "prices": [{"amount": 10, "currency": None}]}],
        )

    with pytest.raises(ValueError, match="at least one pricing plan"):
        TournamentService._pricing("one-time", [])


def test_registration_must_end_before_tournament_starts() -> None:
    registration_end = datetime(2026, 9, 2, tzinfo=UTC)
    start = datetime(2026, 9, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="cannot precede registration end"):
        TournamentService._validate_schedule(None, registration_end, start, None)


def test_registration_finish_is_enforced_only_when_late_registrations_are_ignored() -> None:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    tournament = SimpleNamespace(
        registration_open=True,
        registration_open_override=None,
        status="active",
        finalized_at=now,
        registration_starts_at=None,
        registration_ends_at=datetime(2026, 9, 3, tzinfo=UTC),
        ignore_late_registrations=True,
    )

    assert not TournamentService._registration_is_open(tournament, now)  # type: ignore[arg-type]
    tournament.ignore_late_registrations = False
    assert TournamentService._registration_is_open(tournament, now)  # type: ignore[arg-type]

    tournament.registration_open_override = False
    assert not TournamentService._registration_is_open(tournament, now)  # type: ignore[arg-type]
    tournament.registration_open_override = True
    tournament.ignore_late_registrations = True
    assert TournamentService._registration_is_open(tournament, now)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-09-12T08:59:59+00:00", False),
        ("2026-09-12T09:00:00+00:00", True),
        ("2026-09-12T09:59:59+00:00", True),
        ("2026-09-12T10:00:00+00:00", False),
    ],
)
def test_registration_schedule_boundaries_and_timezone(timestamp, expected) -> None:
    now = datetime.fromisoformat(timestamp)
    tournament = SimpleNamespace(
        status="active", finalized_at=now, registration_open=True,
        registration_open_override=None, ignore_late_registrations=True,
        registration_starts_at=datetime.fromisoformat("2026-09-12T12:00:00+03:00"),
        registration_ends_at=datetime.fromisoformat("2026-09-12T13:00:00+03:00"),
    )
    assert TournamentService._scheduled_registration_is_open(tournament, now) is expected
    assert TournamentService._registration_is_open(tournament, now) is expected
    tournament.registration_open = False
    assert not TournamentService._scheduled_registration_is_open(tournament, now)
    tournament.registration_open_override = True
    assert TournamentService._registration_is_open(tournament, now)
    tournament.finalized_at = None
    assert not TournamentService._registration_is_open(tournament, now)


def test_author_telegram_link_is_normalized_for_account_matching() -> None:
    assert TournamentService._normalize_telegram_link("https://t.me/Example_User") == (
        "https://t.me/Example_User",
        "Example_User",
    )
    assert TournamentService._normalize_telegram_link("") == (None, None)
    with pytest.raises(ValueError, match="Telegram link"):
        TournamentService._normalize_telegram_link("https://example.com/not-telegram")


def test_visible_listing_filters_relationship_and_searches_name_or_slug() -> None:
    items = [
        _listing_item(1, "Autumn Open", membership_status="active"),
        _listing_item(2, "Private Cup", visibility="private", managed=True),
        _listing_item(3, "Spring Cup", phase="future", membership_status="registered"),
    ]
    options = TournamentService._normalize_listing_options(
        phase="ongoing", relationship="participating", search="autumn-open"
    )

    assert TournamentService._filter_visible_items(items, options) == [items[0]]


def test_tournament_listing_cursor_is_bound_to_filters() -> None:
    first = TournamentService._normalize_listing_options(search="cup", order="name_asc")
    second = TournamentService._normalize_listing_options(search="open", order="name_asc")
    cursor = TournamentService._encode_listing_cursor(
        20, TournamentService._listing_signature(first)
    )

    assert (
        TournamentService._decode_listing_cursor(
            cursor, TournamentService._listing_signature(first)
        )
        == 20
    )
    with pytest.raises(ValueError, match="cursor"):
        TournamentService._decode_listing_cursor(
            cursor, TournamentService._listing_signature(second)
        )
