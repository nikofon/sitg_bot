from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class GameSettings:
    """Timing and gameplay options captured when a game is assigned."""

    ready_delay: float = 3.0
    message_delay: float = 3.0
    game_start_to_first_theme_delay: float = 3.0
    theme_to_first_question_delay: float = 3.0
    theme_author_to_commentary_delay: float = 3.0
    theme_commentary_to_question_delay: float = 3.0
    question_cost_announcement_delay: float = 1.0
    question_token_delay: float = 0.6
    question_token_target_chars: int = 18
    buzz_timer_countdown_delay: float = 0.0
    buzz_timeout: float = 10.0
    answer_timeout: float = 15.0
    between_questions_delay: float = 3.0
    last_question_to_theme_complete_delay: float = 3.0
    theme_complete_to_scoreboard_delay: float = 3.0
    between_themes_delay: float = 3.0
    pausing_allowed: bool = True
    question_values: tuple[int, ...] = (10, 20, 30, 40, 50)
    minus_multiplier: float = 1.0
    theme_count: int = 8
    minimum_players: int = 4
    maximum_players: int = 4

    def __post_init__(self) -> None:
        for name in ("minimum_players", "maximum_players"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
                raise ValueError(f"{name} must be between 1 and 12")
        if self.minimum_players > self.maximum_players:
            raise ValueError("minimum_players cannot exceed maximum_players")
        for name in (
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
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if isinstance(self.question_token_target_chars, bool) or not isinstance(
            self.question_token_target_chars, int
        ):
            raise ValueError("question_token_target_chars must be an integer")
        if not 1 <= self.question_token_target_chars <= 200:
            raise ValueError("question_token_target_chars must be between 1 and 200")
        if not isinstance(self.pausing_allowed, bool):
            raise ValueError("pausing_allowed must be true or false")
        if not isinstance(self.question_values, (tuple, list)):
            raise ValueError("question_values must be an ordered list of integers")
        normalized_values = tuple(self.question_values)
        if (
            not normalized_values
            or len(normalized_values) > 128
            or any(
                isinstance(value, bool) or not isinstance(value, int) for value in normalized_values
            )
            or any(not 1 <= value <= 1_000_000_000 for value in normalized_values)
            or tuple(sorted(set(normalized_values))) != normalized_values
        ):
            raise ValueError(
                "question_values must contain 1–128 unique increasing positive integers"
            )
        object.__setattr__(self, "question_values", normalized_values)
        if (
            isinstance(self.minus_multiplier, bool)
            or not isinstance(self.minus_multiplier, (int, float))
            or not math.isfinite(self.minus_multiplier)
            or self.minus_multiplier < 0
            or self.minus_multiplier > 1_000_000
        ):
            raise ValueError("minus_multiplier must be a finite non-negative number")
        if Decimal(str(self.minus_multiplier)).as_tuple().exponent < -8:
            raise ValueError("minus_multiplier supports at most eight decimal places")
        if (
            isinstance(self.theme_count, bool)
            or not isinstance(self.theme_count, int)
            or not 1 <= self.theme_count <= 128
        ):
            raise ValueError("theme_count must be between 1 and 128")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["question_values"] = list(self.question_values)
        return result

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> GameSettings:
        if not values:
            return cls()
        known = {field: values[field] for field in cls.__dataclass_fields__ if field in values}
        return cls(**known)

    def updated(self, changes: Mapping[str, Any]) -> GameSettings:
        unknown = set(changes) - set(self.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown game setting: {sorted(unknown)[0]}")
        return replace(self, **changes)

    @property
    def ready_delta(self) -> timedelta:
        return timedelta(seconds=self.ready_delay)

    @property
    def message_delta(self) -> timedelta:
        return timedelta(seconds=self.message_delay)

    @property
    def token_delta(self) -> timedelta:
        return timedelta(seconds=self.question_token_delay)

    @property
    def game_start_to_first_theme_delta(self) -> timedelta:
        return timedelta(seconds=self.game_start_to_first_theme_delay)

    @property
    def theme_to_first_question_delta(self) -> timedelta:
        return timedelta(seconds=self.theme_to_first_question_delay)

    @property
    def theme_author_to_commentary_delta(self) -> timedelta:
        return timedelta(seconds=self.theme_author_to_commentary_delay)

    @property
    def theme_commentary_to_question_delta(self) -> timedelta:
        return timedelta(seconds=self.theme_commentary_to_question_delay)

    @property
    def question_cost_announcement_delta(self) -> timedelta:
        return timedelta(seconds=self.question_cost_announcement_delay)

    @property
    def buzz_timer_countdown_delta(self) -> timedelta:
        return timedelta(seconds=self.buzz_timer_countdown_delay)

    @property
    def buzz_delta(self) -> timedelta:
        return timedelta(seconds=self.buzz_timeout)

    @property
    def answer_delta(self) -> timedelta:
        return timedelta(seconds=self.answer_timeout)

    @property
    def between_questions_delta(self) -> timedelta:
        return timedelta(seconds=self.between_questions_delay)

    @property
    def last_question_to_theme_complete_delta(self) -> timedelta:
        return timedelta(seconds=self.last_question_to_theme_complete_delay)

    @property
    def theme_complete_to_scoreboard_delta(self) -> timedelta:
        return timedelta(seconds=self.theme_complete_to_scoreboard_delay)

    @property
    def between_themes_delta(self) -> timedelta:
        return timedelta(seconds=self.between_themes_delay)


def announcement_tokens(text: str, target_chars: int) -> tuple[str, ...]:
    """Pack short words into readable chunks while leaving long words alone."""

    words = text.split()
    if not words:
        return ()
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        added_length = len(word) + (1 if current else 0)
        if current and current_length + added_length > target_chars:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
        current.append(word)
        current_length += len(word) + (1 if len(current) > 1 else 0)
        if len(word) >= target_chars:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
    if current:
        chunks.append(" ".join(current))
    return tuple(chunks)
