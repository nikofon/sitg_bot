from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.services.players import (
    AccountSnapshot,
    PlayerAccountService,
    ProfileVersionConflict,
)
from sitg_bot.storage.models import PlayerRecord


class FakeSession:
    def __init__(self, player: PlayerRecord) -> None:
        self.player = player
        self.flush = AsyncMock()

    async def scalar(self, _query: object) -> PlayerRecord:
        return self.player


class FakeDatabase:
    def __init__(self, player: PlayerRecord) -> None:
        self.session = FakeSession(player)

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self.session


def player_record(*, status: str = "registration") -> PlayerRecord:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    return PlayerRecord(
        id=UUID(int=1),
        telegram_user_id=42,
        real_name=None if status == "registration" else "Иван Иванов",
        public_nickname=None if status == "registration" else "Player <b>One</b>",
        telegram_username=None,
        telegram_public=False,
        preferred_locale="ru",
        registration_step="real_name" if status == "registration" else "complete",
        registration_completed_at=None if status == "registration" else now,
        profile_version=0,
        profile_updated_at=now,
        status=status,
    )


def service_for(player: PlayerRecord) -> PlayerAccountService:
    return PlayerAccountService(FakeDatabase(player))  # type: ignore[arg-type]


def test_profile_validators_preserve_unicode_and_user_markup() -> None:
    assert PlayerAccountService.validate_real_name("  Иван   Иванов  ") == "Иван Иванов"
    assert PlayerAccountService.validate_nickname(" Player <b>One</b> ") == "Player <b>One</b>"
    assert PlayerAccountService.validate_telegram_username("@player_name") == "player_name"

    with pytest.raises(ValueError, match="at least two parts"):
        PlayerAccountService.validate_real_name("Prince")
    with pytest.raises(ValueError, match="'ru' or 'en'"):
        PlayerAccountService.validate_locale("de")
    with pytest.raises(ValueError, match="cannot exceed"):
        PlayerAccountService.validate_nickname("Я" * 201)
    with pytest.raises(ValueError, match="unsupported characters"):
        PlayerAccountService.validate_nickname("Player\u0000Name")


@pytest.mark.parametrize(
    ("step", "expected_next", "completed"),
    (
        ("real_name", "real_name", ()),
        ("nickname", "nickname", ("real_name",)),
        ("telegram_public", "telegram_public", ("real_name", "nickname")),
        ("complete", None, ("real_name", "nickname", "telegram_public")),
    ),
)
def test_registration_restart_resumes_every_persisted_step(
    step: str, expected_next: str | None, completed: tuple[str, ...]
) -> None:
    player = player_record()
    player.registration_step = step

    draft = PlayerAccountService._registration_draft(player)

    assert draft.next_step == expected_next
    assert draft.completed_steps == completed


async def test_registration_is_resumable_and_completes_atomically() -> None:
    player = player_record()
    service = service_for(player)

    draft = await service.save_registration_step(
        42, step="real_name", value="Иван Иванов", expected_version=0
    )
    assert draft.next_step == "nickname"
    assert draft.completed_steps == ("real_name",)

    # Locale changes are allowed without losing registration progress.
    draft = await service.update_setting(42, key="language", value="en", expected_version=1)
    assert draft.next_step == "nickname"
    assert player.preferred_locale == "en"

    await service.save_registration_step(
        42, step="nickname", value="Public Player", expected_version=2
    )
    draft = await service.save_registration_step(
        42, step="telegram_public", value=False, expected_version=3
    )
    assert draft.next_step is None
    assert draft.completed_steps == ("real_name", "nickname", "telegram_public")
    assert player.status == "registration"

    account = await service.complete_registration(42, expected_version=4)

    assert account.registration_status == "active"
    assert account.registration_completed_at is not None
    assert player.profile_version == 5


async def test_duplicate_setting_write_is_idempotent_but_stale_change_conflicts() -> None:
    player = player_record(status="active")
    service = service_for(player)

    snapshot = await service.update_setting(
        42, key="nickname", value="New nickname", expected_version=0
    )
    duplicate = await service.update_setting(
        42, key="nickname", value="New nickname", expected_version=0
    )

    assert snapshot.profile_version == duplicate.profile_version == 1
    with pytest.raises(ProfileVersionConflict):
        await service.update_setting(
            42, key="nickname", value="Conflicting nickname", expected_version=0
        )


async def test_settings_have_stable_keys_types_and_localized_values() -> None:
    player = player_record(status="active")
    service = service_for(player)

    snapshot = await service.read_settings(42)

    assert [(item.key, item.value_type) for item in snapshot.settings] == [
        ("real_name", "text"),
        ("nickname", "text"),
        ("telegram_public", "boolean"),
        ("language", "enum"),
    ]
    language = snapshot.settings[-1]
    assert language.localized_value == {"ru": "Русский", "en": "Russian"}
    assert [choice.value for choice in language.choices] == ["ru", "en"]


async def test_username_refresh_does_not_change_telegram_consent() -> None:
    player = player_record(status="active")
    player.telegram_public = True
    service = service_for(player)

    account = await service.refresh_telegram_username(42, "@new_username")

    assert player.telegram_username == "new_username"
    assert player.telegram_public is True
    assert account.profile_version == 1


def test_general_account_lookup_projection_cannot_expose_real_name() -> None:
    assert "real_name" not in {field.name for field in fields(AccountSnapshot)}
