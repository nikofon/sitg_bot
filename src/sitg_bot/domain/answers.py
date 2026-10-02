import re
import unicodedata

_RUSSIAN_TO_LATIN = str.maketrans(dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m",
     "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch",
     "", "y", "", "e", "yu", "ya"),
    strict=True,
)))


def normalize_answer(value: str) -> str:
    """Normalize a submitted or accepted answer for exact comparison."""
    value = unicodedata.normalize("NFKC", value).casefold()
    # Keep Russian letters whose breve/diaeresis is part of the letter, not stress.
    value = "".join(
        part
        for char in value
        for part in (char if char in "йё" else unicodedata.normalize("NFD", char))
        if not unicodedata.category(part).startswith("M")
    )
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def matches_accepted_answer(submitted: str, accepted: str) -> bool:
    """Compare normalized answers, allowing transliteration and limited typos."""
    letters = [char for char in accepted if char.isalpha()]
    if letters and all("LATIN" in unicodedata.name(char, "") for char in letters):
        submitted = submitted.translate(_RUSSIAN_TO_LATIN)
    if submitted == accepted:
        return True
    if min(len(submitted), len(accepted)) < 5:
        return False
    if re.findall(r"\d+", submitted) != re.findall(r"\d+", accepted):
        return False
    # One edit per four expected characters, capped at three edits.
    limit = min(3, len(accepted) // 4)
    if abs(len(submitted) - len(accepted)) > limit:
        return False

    # Banded Levenshtein distance keeps work linear even for long submissions.
    previous = {column: column for column in range(min(len(accepted), limit) + 1)}
    for row, char in enumerate(submitted, 1):
        current = {0: row} if row <= limit else {}
        for column in range(max(1, row - limit), min(len(accepted), row + limit) + 1):
            current[column] = min(
                previous.get(column, limit + 1) + 1,
                current.get(column - 1, limit + 1) + 1,
                previous.get(column - 1, limit + 1) + (char != accepted[column - 1]),
            )
        if min(current.values(), default=limit + 1) > limit:
            return False
        previous = current
    return previous.get(len(accepted), limit + 1) <= limit
