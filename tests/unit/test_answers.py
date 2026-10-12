import pytest

from sitg_bot.domain.answers import (
    matches_accepted_answer,
    matches_answer_pattern,
    normalize_answer,
)
from sitg_bot.domain.packet import _expand_answer_variants


def test_answer_normalization_ignores_case_spacing_and_punctuation() -> None:
    assert normalize_answer("  GUIdo—van  Rossum! ") == "guido van rossum"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ｆｕｌｌ－Ｗｉｄｔｈ", "full width"),
        ("Straße", "strasse"),
        ("rock'n'roll", "rock n roll"),
        ("Це\u0301нтр Помпиду\u0300", "центр помпиду"),
        ("CAFÉ", "cafe"),
        ("cafe\u0301", "cafe"),
        ("Йо\u0301га и ёж", "йога и ёж"),
        ("и\u0306 е\u0308", "й ё"),
        ("  ", ""),
    ],
)
def test_answer_normalization_is_unicode_aware(value: str, expected: str) -> None:
    assert normalize_answer(value) == expected


@pytest.mark.parametrize("pattern", [
    "(Sir) (Isaac) Newton", "(Sir (Isaac)) Newton", "New (York City)",
    "in(ter)national", "(a)(bc)defgh", "  (One) , (Two)  Three! ",
    "(Apollo) 11", "Apollo (11)", "Apollo (1)1", "Apollo (1)(2)",
    "(Центр) Помпиду", "(Центр) Pushkin", "(Sir) Pushkin", "(Café) Йо́га",
    "(word)", "(())", "(one(two) three", "one(two)) three",
    "и(̆)Paris", "е(̈)ж", "и(́)(̆)", "е(\u034f)(̈)",
    "е(\u0345)(̈)ж", "и(\u0345)(̆)",
])
def test_optional_matching_preserves_expanded_answer_behavior(pattern: str) -> None:
    variants = _expand_answer_variants(pattern)
    submissions = {"", "unrelated", "Пушкин", "Pushkin", "Пампиду", "Apollo 12", "1 2"}
    for variant in variants:
        submissions.update((variant, f" {variant.upper()}! ", variant[1:], variant + "x"))
        if len(variant) > 1:
            submissions.add(variant[:1] + "x" + variant[2:])
    for submission in submissions:
        normalized = normalize_answer(submission)
        expected = any(matches_accepted_answer(normalized, normalize_answer(variant))
                       for variant in variants)
        assert matches_answer_pattern(normalized, pattern) is expected, (pattern, submission)
        expected_exact = normalized in {normalize_answer(variant) for variant in variants}
        assert matches_answer_pattern(normalized, pattern, exact=True) is expected_exact


@pytest.mark.parametrize(("submitted", "pattern", "expected"), [
    ("New City", "New (York City)", False),
    ("City York New", "New (York City)", False),
    ("not Newton", "(Sir) Newton", False),
    ("abcx", "(extra) abcd", False),
    ("abcd", "(extra) abcde", False),
    ("abcde", "(extra) abcdef", True),
    ("Apollo 12", "(The) Apollo 11", False),
    ("Apollo 1 2", "Apollo (1)2", False),
    ("abxcdx", "(long optional text) abcde", False),
    ("Пушкин", "(Alexander) Pushkin", True),
    ("Pushkin", "(Александр) Пушкин", False),
])
def test_optional_matching_keeps_order_typo_budget_and_numbers(
    submitted: str, pattern: str, expected: bool,
) -> None:
    assert matches_answer_pattern(normalize_answer(submitted), pattern) is expected


def test_many_optional_groups_do_not_generate_combinations() -> None:
    pattern = " ".join(f"(word{index})" for index in range(80)) + " Newton"
    assert matches_answer_pattern("newton", pattern)
    assert matches_answer_pattern("newtn", pattern)
    assert not matches_answer_pattern("unrelated", pattern)
    assert matches_answer_pattern("newton", pattern, exact=True)
    assert not matches_answer_pattern("newtn", pattern, exact=True)
    complete = normalize_answer(pattern.replace("(", "").replace(")", ""))
    assert matches_answer_pattern(complete, pattern)
    assert matches_answer_pattern("word0 word79 newton", pattern)
    assert matches_answer_pattern("word " * 40 + "newton", "(word) " * 80 + "Newton")


def test_deeply_nested_optional_groups_do_not_recurse() -> None:
    pattern = "(" * 1500 + "Sir" + ")" * 1500 + " Newton"
    assert matches_answer_pattern("newton", pattern)
    assert matches_answer_pattern("sir newton", pattern)
