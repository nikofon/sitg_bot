from __future__ import annotations

import random
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sitg_bot.domain.answers import matches_accepted_answer, normalize_answer
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Packet


class RulesetParameters(Protocol):
    def to_dict(self) -> dict[str, Any]: ...

    def updated(self, changes: Mapping[str, Any]) -> RulesetParameters: ...


class GameRuleset(Protocol):
    """Ruleset-owned behavior used by the shared game host."""

    key: str
    version: int
    parameter_names: frozenset[str]
    parameter_definitions: tuple[ParameterDefinition, ...]

    def packet_editor(self, parameters: RulesetParameters) -> Mapping[str, Any]: ...

    def parameters(self, values: Mapping[str, Any] | None = None) -> RulesetParameters: ...

    def validate_content(
        self, packet: Packet, parameters: RulesetParameters
    ) -> tuple[str, ...]: ...

    def score_answer(self, value: int, correct: bool, parameters: RulesetParameters) -> Decimal: ...

    def judge_answer(
        self,
        submitted: str,
        accepted_answers: Sequence[str],
        *,
        rejected_answers: Sequence[str] = (),
    ) -> bool: ...

    def ranking_key(
        self,
        *,
        score: Decimal,
        correct_values: Sequence[int],
        parameters: RulesetParameters,
    ) -> tuple[Any, ...]: ...

    def validate_lobby(
        self,
        *,
        player_count: int,
        packet_count: int,
        available_play_units: Sequence[PlayUnit],
        parameters: RulesetParameters,
    ) -> tuple[ValidationViolation, ...]: ...

    def prepare_assignment(
        self,
        *,
        player_count: int,
        packet_version_ids: Sequence[UUID],
        available_play_units: Sequence[PlayUnit],
        parameters: RulesetParameters,
        seed: str | None = None,
    ) -> AssignmentPlan: ...


@dataclass(frozen=True, slots=True)
class ParameterDefinition:
    name: str
    value_type: str
    description_key: str


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    code: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExposureClaim:
    namespace: str
    identity: UUID


@dataclass(frozen=True, slots=True)
class PlayUnit:
    kind: str
    logical_id: UUID
    revision_id: UUID
    packet_version_id: UUID
    packet_order: int
    position: int
    question_revision_ids: tuple[UUID, ...]
    claims: tuple[ExposureClaim, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "logical_id": str(self.logical_id),
            "revision_id": str(self.revision_id),
            "packet_version_id": str(self.packet_version_id),
            "packet_order": self.packet_order,
            "position": self.position,
            "question_revision_ids": [str(item) for item in self.question_revision_ids],
            "claims": [
                {"namespace": claim.namespace, "identity": str(claim.identity)}
                for claim in self.claims
            ],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AssignmentPlan:
    ruleset_key: str
    ruleset_version: int
    parameters: Mapping[str, Any]
    packet_version_ids: tuple[UUID, ...]
    play_units: tuple[PlayUnit, ...]
    seed: str
    initial_state: Mapping[str, Any]

    @property
    def claims(self) -> tuple[ExposureClaim, ...]:
        return tuple(claim for unit in self.play_units for claim in unit.claims)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleset_key": self.ruleset_key,
            "ruleset_version": self.ruleset_version,
            "parameters": dict(self.parameters),
            "packet_version_ids": [str(item) for item in self.packet_version_ids],
            "play_units": [unit.to_dict() for unit in self.play_units],
            "seed": self.seed,
            "initial_state": dict(self.initial_state),
        }


@dataclass(frozen=True, slots=True)
class SIGameRuleset:
    key: str = "si"
    version: int = 1

    @property
    def parameter_names(self) -> frozenset[str]:
        return frozenset(GameSettings.__dataclass_fields__)

    @property
    def parameter_definitions(self) -> tuple[ParameterDefinition, ...]:
        number_names = {
            "ready_delay",
            "message_delay",
            "game_start_to_first_theme_delay",
            "theme_to_first_question_delay",
            "theme_author_to_commentary_delay",
            "theme_commentary_to_question_delay",
            "question_cost_announcement_delay",
            "question_token_delay",
            "buzz_timer_countdown_delay",
            "buzz_timeout",
            "answer_timeout",
            "between_questions_delay",
            "last_question_to_theme_complete_delay",
            "theme_complete_to_scoreboard_delay",
            "between_themes_delay",
            "minus_multiplier",
        }
        integer_names = {
            "question_token_target_chars", "theme_count", "minimum_players", "maximum_players"
        }
        return tuple(
            ParameterDefinition(
                name,
                (
                    "number"
                    if name in number_names
                    else "integer"
                    if name in integer_names
                    else "boolean"
                    if name == "pausing_allowed"
                    else "array"
                ),
                f"ruleset.si.{name}.description",
            )
            for name in GameSettings.__dataclass_fields__
        )

    def parameters(self, values: Mapping[str, Any] | None = None) -> GameSettings:
        return GameSettings.from_mapping(values)

    def packet_editor(self, parameters: RulesetParameters) -> Mapping[str, Any]:
        settings = self._settings(parameters)
        return {
            "schema": "si.packet.v1",
            "page_collection": "themes",
            "packet_fields": ("name", "language", "lead_author", "year"),
            "theme_fields": ("name", "author", "commentary"),
            "question_fields": (
                "value",
                "form",
                "text",
                "answer",
                "accepted_answers",
                "rejected_answers",
                "commentary",
                "source",
                "author",
            ),
            "question_values": settings.question_values,
        }

    def validate_content(self, packet: Packet, parameters: RulesetParameters) -> tuple[str, ...]:
        settings = self._settings(parameters)
        errors: list[str] = []
        expected = settings.question_values
        for position, theme in enumerate(packet.themes, 1):
            actual = tuple(question.value for question in theme.questions)
            if actual not in (expected, (0, *expected)):
                errors.append(f"Theme {position} has question values {actual}; expected {expected}")
        return tuple(errors)

    def score_answer(self, value: int, correct: bool, parameters: RulesetParameters) -> Decimal:
        settings = self._settings(parameters)
        if value == 0:
            return Decimal(0)
        points = Decimal(value)
        if correct:
            return points
        return -(points * Decimal(str(settings.minus_multiplier)))

    def judge_answer(
        self,
        submitted: str,
        accepted_answers: Sequence[str],
        *,
        rejected_answers: Sequence[str] = (),
    ) -> bool:
        normalized = normalize_answer(submitted)
        if rejected_answers and normalized in {
            normalize_answer(answer) for answer in rejected_answers
        }:
            return False
        return any(
            matches_accepted_answer(normalized, normalize_answer(answer))
            for answer in accepted_answers
        )

    def ranking_key(
        self,
        *,
        score: Decimal,
        correct_values: Sequence[int],
        parameters: RulesetParameters,
    ) -> tuple[Any, ...]:
        settings = self._settings(parameters)
        correct_values = tuple(value for value in correct_values if value != 0)
        no_minuses = sum(correct_values)
        counts = tuple(
            correct_values.count(value) for value in reversed(settings.question_values[1:])
        )
        return score, no_minuses, *counts

    def validate_lobby(
        self,
        *,
        player_count: int,
        packet_count: int,
        available_play_units: Sequence[PlayUnit],
        parameters: RulesetParameters,
    ) -> tuple[ValidationViolation, ...]:
        settings = self._settings(parameters)
        violations: list[ValidationViolation] = []
        if not 1 <= player_count <= 12:
            violations.append(
                ValidationViolation(
                    "ruleset_player_limit_exceeded",
                    {"minimum": 1, "maximum": 12, "actual": player_count},
                )
            )
        if packet_count < 1:
            violations.append(ValidationViolation("packet_not_playable", {"reason": "missing"}))
        incompatible = [
            str(unit.revision_id)
            for unit in available_play_units
            if tuple(unit.metadata.get("question_values", ()))
            not in (settings.question_values, (0, *settings.question_values))
        ]
        if incompatible:
            violations.append(
                ValidationViolation(
                    "packet_content_incompatible",
                    {"play_unit_revision_ids": incompatible},
                )
            )
        if len(available_play_units) < settings.theme_count:
            violations.append(
                ValidationViolation(
                    "insufficient_fresh_content",
                    {"required": settings.theme_count, "available": len(available_play_units)},
                )
            )
        return tuple(violations)

    def prepare_assignment(
        self,
        *,
        player_count: int,
        packet_version_ids: Sequence[UUID],
        available_play_units: Sequence[PlayUnit],
        parameters: RulesetParameters,
        seed: str | None = None,
    ) -> AssignmentPlan:
        settings = self._settings(parameters)
        violations = self.validate_lobby(
            player_count=player_count,
            packet_count=len(packet_version_ids),
            available_play_units=available_play_units,
            parameters=parameters,
        )
        if violations:
            raise ValueError(violations[0].code)
        stored_seed = seed or secrets.token_hex(32)
        chosen = random.Random(stored_seed).sample(list(available_play_units), settings.theme_count)
        ordered = tuple(sorted(chosen, key=lambda unit: (unit.packet_order, unit.position)))
        return AssignmentPlan(
            ruleset_key=self.key,
            ruleset_version=self.version,
            parameters=settings.to_dict(),
            packet_version_ids=tuple(packet_version_ids),
            play_units=ordered,
            seed=stored_seed,
            initial_state={"phase": "lobby", "next_play_unit": 0},
        )

    @staticmethod
    def _settings(parameters: RulesetParameters) -> GameSettings:
        if not isinstance(parameters, GameSettings):
            raise TypeError("SI requires SI game settings")
        return parameters


class GameRulesetRegistry:
    def __init__(self, rulesets: Iterable[GameRuleset] = ()) -> None:
        self._rulesets: dict[tuple[str, int], GameRuleset] = {}
        for ruleset in rulesets:
            self.register(ruleset)

    def register(self, ruleset: GameRuleset) -> None:
        identity = (ruleset.key, ruleset.version)
        if identity in self._rulesets:
            raise ValueError(f"Ruleset {ruleset.key} version {ruleset.version} is registered")
        self._rulesets[identity] = ruleset

    def get(self, key: str, version: int) -> GameRuleset:
        try:
            return self._rulesets[(key, version)]
        except KeyError as error:
            raise LookupError(f"Unsupported game ruleset: {key} version {version}") from error


DEFAULT_RULESETS = GameRulesetRegistry((SIGameRuleset(),))
