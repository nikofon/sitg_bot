import re
import unicodedata

_RUSSIAN_TO_LATIN = str.maketrans(dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m",
     "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch",
     "", "y", "", "e", "yu", "ya"),
    strict=True,
)))


def _normalize_characters(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # Keep Russian letters whose breve/diaeresis is part of the letter, not stress.
    value = "".join(
        part
        for char in value
        for part in (char if char in "йё" else unicodedata.normalize("NFD", char))
        if not unicodedata.category(part).startswith("M")
    )
    return re.sub(r"[^\w\s]", " ", value)


def normalize_answer(value: str) -> str:
    """Normalize a submitted or accepted answer for exact comparison."""
    return " ".join(_normalize_characters(value).split())


def _pattern_characters(value: str):
    # Retain combining marks that can affect a Russian letter across an optional boundary.
    for original in unicodedata.normalize("NFKC", value):
        if original == "\u0345":  # Case folding emits an iota after the base letter.
            yield original
            continue
        for char in original.casefold():
            for part in (char if char in "йё" else unicodedata.normalize("NFD", char)):
                if unicodedata.category(part).startswith("M"):
                    if unicodedata.combining(part) in {0, 230}:
                        yield part
                elif part.isalnum() or part == "_" or part.isspace():
                    yield part
                else:
                    yield " "


def _optional_pattern(answer: str) -> tuple[list[str | None], dict[int, int]]:
    """Compile literal characters and forward jumps over balanced optional groups."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, char in enumerate(answer):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            pairs[stack.pop()] = index
    if not pairs:
        return [], {}

    boundaries = set(pairs) | set(pairs.values())
    positions: dict[int, int] = {}
    characters: list[str | None] = []
    start = 0
    for index in sorted(boundaries):
        characters.extend(_pattern_characters(answer[start:index]))
        positions[index] = len(characters)
        characters.append(None)
        start = index + 1
    characters.extend(_pattern_characters(answer[start:]))
    return characters, {positions[left]: positions[right] + 1 for left, right in pairs.items()}


# Submission position, candidate length, letter kind (none/Latin/other), number position,
# whether a number is open, pending whitespace, and a pending Russian base letter.
_MatchState = tuple[int, int, int, int, bool, bool, str]


def _matches_optional_pattern(
    submitted: str,
    characters: list[str | None],
    jumps: dict[int, int],
    *,
    exact: bool,
    latin_only: bool | None,
) -> bool:
    maximum = 0 if exact or len(submitted) < 5 else 3
    numbers = "".join(number + "\0" for number in re.findall(r"\d+", submitted))
    rows: dict[int, dict[_MatchState, int]] = {0: {(0, 0, 0, 0, False, False, ""): 0}}

    def store(row: dict[_MatchState, int], state: _MatchState, cost: int) -> None:
        if cost < row.get(state, maximum + 1):
            row[state] = cost

    def consume(state: _MatchState, cost: int, char: str):
        position, length, letters, number, in_number, _, _ = state
        if length >= len(submitted) + maximum:
            return
        if char.isdecimal():
            if number >= len(numbers) or numbers[number] != char:
                return
            number += 1
            in_number = True
        elif in_number:
            if number >= len(numbers) or numbers[number] != "\0":
                return
            number += 1
            in_number = False
        if char.isalpha():
            kind = 1 if "LATIN" in unicodedata.name(char, "") else 2
            letters = max(letters, kind)
        for inserted in range(min(maximum - cost, len(submitted) - position) + 1):
            index = position + inserted
            distance = cost + inserted
            if distance < maximum:
                yield (index, length + 1, letters, number, in_number, False, ""), distance + 1
            if index < len(submitted):
                distance += char != submitted[index]
                if distance <= maximum:
                    yield (index + 1, length + 1, letters, number, in_number, False, ""), distance

    def emit(state: _MatchState, cost: int, char: str):
        if char.isspace():
            position, length, letters, number, in_number, _, _ = state
            yield (position, length, letters, number, in_number, bool(length), ""), cost
        else:
            prefixes = consume(state, cost, " ") if state[-2] else ((state, cost),)
            for prefix, distance in prefixes:
                yield from consume(prefix, distance, char)

    def flush(state: _MatchState, cost: int):
        if state[-1]:
            pending = state[-1].replace("!", "")
            current = {state: cost}
            for char in pending:
                following: dict[_MatchState, int] = {}
                for prefix, distance in current.items():
                    for result, total in emit(prefix, distance, char):
                        store(following, result, total)
                current = following
            yield from current.items()
        else:
            yield state, cost

    for index, char in enumerate(characters):
        row = rows.pop(index, {})
        if not row:
            if not rows:
                return False
            continue
        next_row = rows.setdefault(index + 1, {})
        if char is None:
            for state, cost in row.items():
                store(next_row, state, cost)
                if index in jumps:
                    store(rows.setdefault(jumps[index], {}), state, cost)
        else:
            for state, cost in row.items():
                if unicodedata.category(char).startswith("M"):
                    pending = state[-1]
                    if char == "\u0345":
                        if pending:
                            if len(pending) <= len(submitted) + maximum:
                                store(next_row, (*state[:-1], pending + "ι"), cost)
                        else:
                            for result, total in emit(state, cost, "ι"):
                                store(next_row, result, total)
                    elif unicodedata.combining(char) == 0:
                        for result, total in flush(state, cost):
                            store(next_row, result, total)
                    else:
                        if pending and pending[0] in "ие" and "!" not in pending:
                            composed = {("и", "\u0306"): "й", ("е", "\u0308"): "ё"}
                            base = composed.get((pending[0], char))
                            pending = base + pending[1:] if base else pending + "!"
                        store(next_row, (*state[:-1], pending), cost)
                    continue
                for prefix, distance in flush(state, cost):
                    if char in "ие":
                        store(next_row, (*prefix[:-1], char), distance)
                    else:
                        for result, total in emit(prefix, distance, char):
                            store(next_row, result, total)

    final: dict[_MatchState, int] = {}
    for state, cost in rows.get(len(characters), {}).items():
        for result, total in flush(state, cost):
            store(final, result, total)
    for state, cost in final.items():
        position, length, letters, number, in_number, _, _ = state
        if in_number:
            if number >= len(numbers) or numbers[number] != "\0":
                continue
            number += 1
        if number != len(numbers) or latin_only is not None and (letters == 1) != latin_only:
            continue
        cost += len(submitted) - position
        limit = 0 if exact or min(len(submitted), length) < 5 else min(3, length // 4)
        if cost <= limit:
            return True
    return False


def matches_answer_pattern(submitted: str, answer: str, *, exact: bool = False) -> bool:
    """Match a normalized submission without enumerating optional-text combinations."""
    characters, jumps = _optional_pattern(answer)
    if not jumps:
        accepted = normalize_answer(answer)
        return submitted == accepted if exact else matches_accepted_answer(submitted, accepted)
    if exact:
        return _matches_optional_pattern(
            submitted, characters, jumps, exact=True, latin_only=None,
        )
    transliterated = submitted.translate(_RUSSIAN_TO_LATIN)
    if transliterated == submitted:
        return _matches_optional_pattern(
            submitted, characters, jumps, exact=False, latin_only=None,
        )
    return _matches_optional_pattern(
        submitted, characters, jumps, exact=False, latin_only=False,
    ) or _matches_optional_pattern(
        transliterated, characters, jumps, exact=False, latin_only=True,
    )


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
