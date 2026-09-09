import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentRegistrationAttemptRecord,
)

SUPPORTED_LOCALES = ("ru", "en")
REGISTRATION_STEPS = ("real_name", "nickname", "telegram_public")
_NEXT_REGISTRATION_STEP = {
    "real_name": "nickname",
    "nickname": "telegram_public",
    "telegram_public": "complete",
}
_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ProfileVersionConflict(ValueError):
    """Raised when a mutable profile was changed after the caller read it."""


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    player_id: UUID
    telegram_user_id: int | None
    public_nickname: str | None
    preferred_locale: str
    registration_status: str
    registration_completed_at: datetime | None
    profile_version: int


@dataclass(frozen=True, slots=True)
class RegistrationDraft:
    account: AccountSnapshot
    next_step: str | None
    completed_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettingChoice:
    value: str | bool
    labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class SettingDescriptor:
    key: str
    value_type: str
    labels: dict[str, str]
    value: str | bool
    localized_value: dict[str, str]
    choices: tuple[SettingChoice, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    profile_version: int
    updated_at: datetime
    settings: tuple[SettingDescriptor, ...]


@dataclass(frozen=True, slots=True)
class PrivatePlayerIdentity:
    player_id: UUID
    real_name: str
    public_nickname: str


class PlayerAccountService:
    """Owns Telegram identity, resumable registration, and private profile settings."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def lookup_by_telegram_user_id(self, telegram_user_id: int) -> AccountSnapshot | None:
        async with self.database.transaction() as session:
            player = await self._player_by_telegram_id(session, telegram_user_id)
            return None if player is None else self._account_snapshot(player)

    async def create_or_resume_registration(
        self, telegram_user_id: int, *, telegram_username: str | None = None
    ) -> RegistrationDraft:
        normalized_username = self.validate_telegram_username(telegram_username)
        async with self.database.transaction() as session:
            # A Telegram user can race through bot and Mini App entry points. The
            # transaction-scoped lock prevents two draft rows from being inserted.
            await session.execute(select(func.pg_advisory_xact_lock(telegram_user_id)))
            player = await self._player_by_telegram_id(
                session, telegram_user_id, with_for_update=True
            )
            now = datetime.now(UTC)
            if player is None:
                player = PlayerRecord(
                    telegram_user_id=telegram_user_id,
                    telegram_username=normalized_username,
                    telegram_username_updated_at=(now if normalized_username else None),
                    status="registration",
                )
                session.add(player)
                await session.flush()
            elif player.status == "anonymized":
                raise PermissionError("Player account is anonymized")
            elif player.telegram_username != normalized_username:
                self._set_telegram_username(player, normalized_username, now=now)
            return self._registration_draft(player)

    async def save_registration_step(
        self,
        telegram_user_id: int,
        *,
        step: str,
        value: object,
        expected_version: int,
    ) -> RegistrationDraft:
        if step not in REGISTRATION_STEPS:
            raise ValueError("Unknown registration step")
        normalized = self._validated_setting_value(step, value)
        async with self.database.transaction() as session:
            player = await self._required_player_by_telegram_id(
                session, telegram_user_id, with_for_update=True
            )
            if player.status != "registration":
                if (
                    player.status == "active"
                    and self._registration_value(player, step) == normalized
                ):
                    return self._registration_draft(player)
                raise PermissionError("Player is not registering")
            if player.profile_version != expected_version:
                if self._registration_value(
                    player, step
                ) == normalized and REGISTRATION_STEPS.index(step) < self._registration_position(
                    player.registration_step
                ):
                    return self._registration_draft(player)
                raise ProfileVersionConflict("Player profile has changed")
            if player.registration_step != step:
                raise ValueError("Registration step is not currently expected")

            if step == "real_name":
                player.real_name = str(normalized)
            elif step == "nickname":
                player.public_nickname = str(normalized)
            else:
                player.telegram_public = bool(normalized)
            player.registration_step = _NEXT_REGISTRATION_STEP[step]
            self._touch_profile(player)
            await session.flush()
            return self._registration_draft(player)

    async def complete_registration(
        self, telegram_user_id: int, *, expected_version: int
    ) -> AccountSnapshot:
        async with self.database.transaction() as session:
            player = await self._required_player_by_telegram_id(
                session, telegram_user_id, with_for_update=True
            )
            if player.status == "active":
                return self._account_snapshot(player)
            if player.status != "registration":
                raise PermissionError("Player registration cannot be completed")
            if player.profile_version != expected_version:
                raise ProfileVersionConflict("Player profile has changed")
            if player.registration_step != "complete":
                raise ValueError("Registration is incomplete")

            # Revalidate the complete aggregate in the same transaction that
            # activates it; no partially valid account can become active.
            player.real_name = self.validate_real_name(player.real_name)
            player.public_nickname = self.validate_nickname(player.public_nickname)
            self.validate_locale(player.preferred_locale)
            player.status = "active"
            player.registration_completed_at = datetime.now(UTC)
            self._touch_profile(player)
            await session.flush()
            return self._account_snapshot(player)

    async def read_settings(self, telegram_user_id: int) -> SettingsSnapshot:
        async with self.database.transaction() as session:
            player = await self._required_player_by_telegram_id(session, telegram_user_id)
            if player.status != "active":
                raise PermissionError("Registration is required")
            return self._settings_snapshot(player)

    async def update_setting(
        self,
        telegram_user_id: int,
        *,
        key: str,
        value: object,
        expected_version: int,
    ) -> SettingsSnapshot | RegistrationDraft:
        field = {
            "real_name": "real_name",
            "nickname": "public_nickname",
            "telegram_public": "telegram_public",
            "language": "preferred_locale",
        }.get(key)
        if field is None:
            raise ValueError("Unknown setting")
        normalized = self._validated_setting_value(key, value)
        async with self.database.transaction() as session:
            player = await self._required_player_by_telegram_id(
                session, telegram_user_id, with_for_update=True
            )
            if player.status == "registration" and key != "language":
                raise PermissionError("Complete registration before editing this setting")
            if player.status not in {"registration", "active"}:
                raise PermissionError("Player account is not active")

            current = getattr(player, field)
            if player.profile_version != expected_version:
                if current == normalized:
                    return (
                        self._settings_snapshot(player)
                        if player.status == "active"
                        else self._registration_draft(player)
                    )
                raise ProfileVersionConflict("Player profile has changed")
            if current != normalized:
                setattr(player, field, normalized)
                self._touch_profile(player)
                await session.flush()
            return (
                self._settings_snapshot(player)
                if player.status == "active"
                else self._registration_draft(player)
            )

    async def refresh_telegram_username(
        self, telegram_user_id: int, telegram_username: str | None
    ) -> AccountSnapshot:
        normalized = self.validate_telegram_username(telegram_username)
        async with self.database.transaction() as session:
            player = await self._required_player_by_telegram_id(
                session, telegram_user_id, with_for_update=True
            )
            if player.telegram_username != normalized:
                self._set_telegram_username(player, normalized, now=datetime.now(UTC))
                await session.flush()
            return self._account_snapshot(player)

    async def read_real_name_for_organizer(
        self, organizer_id: UUID, target_player_id: UUID, *, tournament_id: UUID
    ) -> PrivatePlayerIdentity:
        async with self.database.transaction() as session:
            manager = await session.get(TournamentManagerRecord, (tournament_id, organizer_id))
            if manager is None or manager.revoked_at is not None:
                raise PermissionError("Tournament manager role is required")
            membership = await session.get(
                TournamentMembershipRecord, (tournament_id, target_player_id)
            )
            has_attempt = await session.scalar(
                select(TournamentRegistrationAttemptRecord.id)
                .where(
                    TournamentRegistrationAttemptRecord.tournament_id == tournament_id,
                    TournamentRegistrationAttemptRecord.player_id == target_player_id,
                )
                .limit(1)
            )
            has_membership_relationship = membership is not None and membership.status != "invited"
            if not has_membership_relationship and has_attempt is None:
                raise PermissionError("Player has no relationship with this tournament")
            return await self._private_identity(session, target_player_id)

    async def read_real_name_for_administrator(
        self, administrator_id: UUID, target_player_id: UUID, *, purpose: str
    ) -> PrivatePlayerIdentity:
        if not purpose.strip():
            raise ValueError("A real-name access purpose is required")
        async with self.database.transaction() as session:
            administrator = await session.get(PlatformAdministratorRecord, administrator_id)
            if administrator is None or administrator.revoked_at is not None:
                raise PermissionError("Platform administrator role is required")
            return await self._private_identity(session, target_player_id)

    @staticmethod
    def validate_real_name(value: object) -> str:
        normalized = PlayerAccountService._normalize_text(value, setting="Real name")
        if len(normalized) > 200:
            raise ValueError("Real name cannot exceed 200 characters")
        if len(normalized.split()) < 2:
            raise ValueError("Real name must contain at least two parts")
        return normalized

    @staticmethod
    def validate_nickname(value: object) -> str:
        normalized = PlayerAccountService._normalize_text(value, setting="Nickname")
        if len(normalized) > 200:
            raise ValueError("Nickname cannot exceed 200 characters")
        return normalized

    @staticmethod
    def validate_locale(value: object) -> str:
        if not isinstance(value, str) or value not in SUPPORTED_LOCALES:
            raise ValueError("Language must be 'ru' or 'en'")
        return value

    @staticmethod
    def validate_telegram_username(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().removeprefix("@")
        if not normalized:
            return None
        if not _TELEGRAM_USERNAME_RE.fullmatch(normalized):
            raise ValueError("Invalid Telegram username")
        return normalized

    @staticmethod
    def _normalize_text(value: object, *, setting: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{setting} must be text")
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError(f"{setting} cannot be empty")
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
            raise ValueError(f"{setting} contains unsupported characters")
        return normalized

    @classmethod
    def _validated_setting_value(cls, key: str, value: object) -> str | bool:
        if key == "real_name":
            return cls.validate_real_name(value)
        if key in {"nickname"}:
            return cls.validate_nickname(value)
        if key in {"language"}:
            return cls.validate_locale(value)
        if key == "telegram_public":
            if not isinstance(value, bool):
                raise ValueError("Telegram visibility must be a boolean")
            return value
        raise ValueError("Unknown setting")

    @staticmethod
    async def _player_by_telegram_id(
        session: AsyncSession, telegram_user_id: int, *, with_for_update: bool = False
    ) -> PlayerRecord | None:
        query = select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
        if with_for_update:
            query = query.with_for_update()
        return await session.scalar(query)

    @classmethod
    async def _required_player_by_telegram_id(
        cls, session: AsyncSession, telegram_user_id: int, *, with_for_update: bool = False
    ) -> PlayerRecord:
        player = await cls._player_by_telegram_id(
            session, telegram_user_id, with_for_update=with_for_update
        )
        if player is None:
            raise LookupError("Player account not found")
        return player

    @staticmethod
    async def _private_identity(session: AsyncSession, player_id: UUID) -> PrivatePlayerIdentity:
        player = await session.get(PlayerRecord, player_id)
        if (
            player is None
            or player.status != "active"
            or player.real_name is None
            or player.public_nickname is None
        ):
            raise LookupError("Active player not found")
        return PrivatePlayerIdentity(player.id, player.real_name, player.public_nickname)

    @staticmethod
    def _touch_profile(player: PlayerRecord) -> None:
        player.profile_version += 1
        player.profile_updated_at = datetime.now(UTC)

    @classmethod
    def _set_telegram_username(
        cls, player: PlayerRecord, username: str | None, *, now: datetime
    ) -> None:
        player.telegram_username = username
        player.telegram_username_updated_at = now
        cls._touch_profile(player)

    @staticmethod
    def _account_snapshot(player: PlayerRecord) -> AccountSnapshot:
        return AccountSnapshot(
            player.id,
            player.telegram_user_id,
            player.public_nickname,
            player.preferred_locale,
            player.status,
            player.registration_completed_at,
            player.profile_version,
        )

    @classmethod
    def _registration_draft(cls, player: PlayerRecord) -> RegistrationDraft:
        position = cls._registration_position(player.registration_step)
        next_step = player.registration_step if player.status == "registration" else None
        if next_step == "complete":
            next_step = None
        return RegistrationDraft(
            cls._account_snapshot(player),
            next_step,
            REGISTRATION_STEPS[:position],
        )

    @staticmethod
    def _registration_position(step: str) -> int:
        return len(REGISTRATION_STEPS) if step == "complete" else REGISTRATION_STEPS.index(step)

    @staticmethod
    def _registration_value(player: PlayerRecord, step: str) -> object:
        if step == "real_name":
            return player.real_name
        if step == "nickname":
            return player.public_nickname
        return player.telegram_public

    @staticmethod
    def _settings_snapshot(player: PlayerRecord) -> SettingsSnapshot:
        assert player.real_name is not None
        assert player.public_nickname is not None
        boolean_choices = (
            SettingChoice(False, {"ru": "Нет", "en": "No"}),
            SettingChoice(True, {"ru": "Да", "en": "Yes"}),
        )
        language_choices = (
            SettingChoice("ru", {"ru": "Русский", "en": "Russian"}),
            SettingChoice("en", {"ru": "Английский", "en": "English"}),
        )
        public_value = {
            "ru": "Да" if player.telegram_public else "Нет",
            "en": "Yes" if player.telegram_public else "No",
        }
        language_value = {
            "ru": "Русский" if player.preferred_locale == "ru" else "Английский",
            "en": "Russian" if player.preferred_locale == "ru" else "English",
        }
        return SettingsSnapshot(
            player.profile_version,
            player.profile_updated_at,
            (
                SettingDescriptor(
                    "real_name",
                    "text",
                    {"ru": "Настоящее имя", "en": "Real name"},
                    player.real_name,
                    {locale: player.real_name for locale in SUPPORTED_LOCALES},
                ),
                SettingDescriptor(
                    "nickname",
                    "text",
                    {"ru": "Имя игрока", "en": "Nickname"},
                    player.public_nickname,
                    {locale: player.public_nickname for locale in SUPPORTED_LOCALES},
                ),
                SettingDescriptor(
                    "telegram_public",
                    "boolean",
                    {"ru": "Публичный Telegram", "en": "Public Telegram"},
                    player.telegram_public,
                    public_value,
                    boolean_choices,
                ),
                SettingDescriptor(
                    "language",
                    "enum",
                    {"ru": "Язык", "en": "Language"},
                    player.preferred_locale,
                    language_value,
                    language_choices,
                ),
            ),
        )
