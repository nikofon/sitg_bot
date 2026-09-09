import pytest

from sitg_bot.domain.answers import normalize_answer


def test_answer_normalization_ignores_case_spacing_and_punctuation() -> None:
    assert normalize_answer("  GUIdo—van  Rossum! ") == "guido van rossum"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ｆｕｌｌ－Ｗｉｄｔｈ", "full width"),
        ("Straße", "strasse"),
        ("rock'n'roll", "rock n roll"),
        ("  ", ""),
    ],
)
def test_answer_normalization_is_unicode_aware(value: str, expected: str) -> None:
    assert normalize_answer(value) == expected
