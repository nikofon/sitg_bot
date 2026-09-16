import re
from dataclasses import dataclass

QUESTION_VALUES = (10, 20, 30, 40, 50)
MAX_CUSTOM_GAME_THEMES = 128
LANGUAGE_TAG_RE = re.compile(r"^(?:und|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$")


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
    form: str = ""
    source: str = ""
    author: str = ""

    @property
    def all_answers(self) -> tuple[str, ...]:
        def expand(answer: str) -> list[str]:
            match = re.search(r"\(([^()]*)\)", answer)
            if match is None:
                return [" ".join(answer.split())]
            before, optional, after = answer[: match.start()], match.group(1), answer[match.end() :]
            return [*expand(before + optional + after), *expand(before + after)]

        return tuple(
            dict.fromkeys(
                variant
                for answer in (self.answer, *self.accepted_answers)
                for variant in expand(answer)
            )
        )


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
            or any(value <= 0 for value in values)
            or tuple(sorted(set(values))) != values
        ):
            raise ValueError("Theme question values must be unique increasing positive integers")


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
