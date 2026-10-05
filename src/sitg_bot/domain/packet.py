import re
from collections.abc import Iterable
from dataclasses import dataclass

QUESTION_VALUES = (10, 20, 30, 40, 50)
MAX_CUSTOM_GAME_THEMES = 128
LANGUAGE_TAG_RE = re.compile(r"^(?:und|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$")
SIMILAR_PACKET_QUESTION_FRACTION = 0.9


def normalize_language_tag(value: str) -> str:
    tag = value.strip()
    if not LANGUAGE_TAG_RE.fullmatch(tag):
        raise ValueError("Language must be 'und' or a valid BCP 47-style tag")
    parts = tag.split("-")
    return "-".join(
        part.lower() if index == 0 else part.upper() if len(part) == 2 else part
        for index, part in enumerate(parts)
    )


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    answer: str
    commentary: str
    value: int
    accepted_answers: tuple[str, ...] = ()
    rejected_answers: tuple[str, ...] = ()
    form: str = ""
    source: str = ""
    author: str = ""

    @property
    def all_answers(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                variant
                for answer in (self.answer, *self.accepted_answers)
                for variant in _expand_answer_variants(answer)
            )
        )

    @property
    def all_rejected_answers(self) -> tuple[str, ...]:
        """Answers that are never correct, checked before accepted answers."""
        return tuple(
            dict.fromkeys(
                variant
                for answer in self.rejected_answers
                for variant in _expand_answer_variants(answer)
            )
        )


def _expand_answer_variants(answer: str) -> list[str]:
    match = re.search(r"\(([^()]*)\)", answer)
    if match is None:
        return [" ".join(answer.split())]
    before, optional, after = answer[: match.start()], match.group(1), answer[match.end() :]
    with_optional = _expand_answer_variants(before + optional + after)
    without_optional = _expand_answer_variants(before + after)
    return [*with_optional, *without_optional]


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    questions: tuple[Question, ...]
    author: str = ""
    commentary: str = ""

    def __post_init__(self) -> None:
        values = tuple(question.value for question in self.questions)
        if (
            not values
            or any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            or any(value < 0 for value in values)
            or tuple(sorted(set(values))) != values
        ):
            raise ValueError(
                "Theme question values must be unique increasing non-negative integers"
            )


@dataclass(frozen=True, slots=True)
class Packet:
    name: str
    themes: tuple[Theme, ...]
    year: int | None = None
    lead_author: str = ""
    language: str = "und"

    def __post_init__(self) -> None:
        if not self.themes:
            raise ValueError("A packet must contain at least one theme")
        if self.year is not None and not 1000 <= self.year <= 9999:
            raise ValueError("Packet year must contain four digits")
        object.__setattr__(self, "language", normalize_language_tag(self.language))


def question_content_fingerprint(text: str, answer: str) -> str:
    """Case- and whitespace-insensitive identity of a question's text and answer."""
    return f"{' '.join(text.casefold().split())}\n{' '.join(answer.casefold().split())}"


def packet_question_fingerprints(packet: Packet) -> frozenset[str]:
    """Distinct question fingerprints, deliberately ignoring packet and author metadata."""
    return frozenset(
        question_content_fingerprint(question.text, question.answer)
        for theme in packet.themes
        for question in theme.questions
    )


def similar_question_share(fingerprints: frozenset[str], existing: Iterable[str]) -> float:
    """Share of distinct draft questions whose text and answer appear in ``existing``."""
    if not fingerprints:
        return 0.0
    return len(fingerprints.intersection(existing)) / len(fingerprints)
