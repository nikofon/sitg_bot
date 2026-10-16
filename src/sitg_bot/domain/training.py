"""Pure training presentation rules; source revisions are never transformed in storage."""

import math
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from sitg_bot.domain.game_settings import announcement_tokens

TOKENIZER_VERSION = "announcement-v1"


@dataclass(frozen=True, slots=True)
class Modifier:
    key: str
    version: int
    interaction_modes: tuple[str, ...]
    incompatible: tuple[str, ...] = ()


MODIFIERS = {
    "reversed": Modifier("reversed", 1, ("incremental", "instant")),
    "hidden": Modifier("hidden", 1, ("instant",)),
    "best_buzz": Modifier("best_buzz", 1, ("incremental",), ("reversed", "hidden")),
}


def display_text(source: str, modifiers: tuple[str, ...] = ()) -> str:
    if any(key not in MODIFIERS for key in modifiers):
        raise ValueError("Unknown display modifier")
    for key in modifiers:
        if set(MODIFIERS[key].incompatible) & set(modifiers):
            raise ValueError("Incompatible display modifiers")
    return " ".join(reversed(source.split())) if "reversed" in modifiers else source


@dataclass(frozen=True, slots=True)
class TrainingSettings:
    version: int = 1
    token_delay: float = 0.6
    token_target_chars: int = 18
    buzz_timeout: float = 10.0
    answer_timeout: float = 15.0
    reversed: bool = False
    hidden_after: float | None = None
    best_buzz: bool = False

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported training settings version")
        for name in ("token_delay", "buzz_timeout", "answer_timeout", "hidden_after"):
            value = getattr(self, name)
            if name == "hidden_after" and value is None:
                continue
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(f"{name} must be a positive number no greater than 3600")
        if type(self.token_target_chars) is not int or not 1 <= self.token_target_chars <= 200:
            raise ValueError("token_target_chars must be between 1 and 200")
        if any(type(getattr(self, name)) is not bool for name in ("reversed", "best_buzz")):
            raise ValueError("Modifier switches must be booleans")
        if self.hidden_after is not None and self.hidden_after >= self.buzz_timeout:
            raise ValueError("Hidden text must disappear before the buzz timer ends")
        if self.best_buzz and (self.reversed or self.hidden_after is not None):
            raise ValueError("Best buzz requires untransformed incremental text")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "TrainingSettings":
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown training setting")
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def tokens(self, source: str) -> tuple[str, ...]:
        displayed = display_text(source, ("reversed",) if self.reversed else ())
        return announcement_tokens(displayed, self.token_target_chars)


def text_digest(source: str) -> str:
    return sha256(source.encode()).hexdigest()


def best_buzz_cursor(
    source: str,
    *,
    revision_id: UUID,
    analysis: dict[str, Any],
    target_chars: int,
) -> int:
    """Convert an exclusive source character boundary to an application token cursor.

    A boundary inside a packed chunk pauses after that chunk. Provider token indexes
    are never used. Whitespace in the original revision is included in offsets.
    """
    if (
        analysis.get("schema_version") != 1
        or analysis.get("tokenizer_version") != TOKENIZER_VERSION
        or analysis.get("question_revision_id") != str(revision_id)
        or analysis.get("text_digest") != text_digest(source)
    ):
        raise ValueError("Best buzz analysis does not match the pinned question")
    boundary = analysis.get("source_end")
    if type(boundary) is not int or not 0 <= boundary <= len(source):
        raise ValueError("Best buzz source boundary is invalid")
    if boundary == 0:
        return 0
    words = list(re.finditer(r"\S+", source))
    if not any(word.end() == boundary for word in words):
        raise ValueError("Best buzz must end at a source word boundary")
    word_count = next(i + 1 for i, word in enumerate(words) if word.end() == boundary)
    consumed = 0
    for cursor, token in enumerate(announcement_tokens(source, target_chars), 1):
        consumed += len(token.split())
        if consumed >= word_count:
            return cursor
    raise ValueError("Best buzz source boundary is unavailable")


def normal_game_modifiers(values: tuple[str, ...], *, allowed: tuple[str, ...] = ()) -> None:
    """Policy gate for future normal-game display settings; disabled by default."""
    if any(value not in MODIFIERS or value not in allowed for value in values):
        raise ValueError("Display modifier is disabled by tournament policy")
