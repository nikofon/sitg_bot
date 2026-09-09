import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.packet_import import (
    packet_from_data,
    packet_from_docx,
    packet_from_docx_bytes,
    packet_from_json,
    packet_to_json,
)


def test_parenthesized_answer_parts_are_optional() -> None:
    question = Question("Text", "(Guido) Van Rossum", "Commentary", 10)

    assert question.all_answers == ("Guido Van Rossum", "Van Rossum")


def test_nested_optional_answer_parts_expand_and_deduplicate() -> None:
    question = Question(
        "Text",
        "(Sir) (Isaac) Newton",
        "",
        10,
        accepted_answers=("Isaac Newton",),
    )

    assert question.all_answers == (
        "Sir Isaac Newton",
        "Sir Newton",
        "Isaac Newton",
        "Newton",
    )


def test_theme_accepts_ruleset_configured_values_but_requires_increasing_order() -> None:
    assert Theme("Valid", (Question("Text", "Answer", "Commentary", 100),)).questions

    with pytest.raises(ValueError, match="unique increasing positive"):
        Theme(
            "Invalid",
            (
                Question("First", "Answer", "Commentary", 200),
                Question("Second", "Answer", "Commentary", 100),
            ),
        )


def test_packet_requires_themes_and_a_four_digit_year() -> None:
    with pytest.raises(ValueError, match="at least one theme"):
        Packet("Empty", ())

    theme = Theme(
        "Theme",
        tuple(Question(str(value), "Answer", "", value) for value in (10, 20, 30, 40, 50)),
    )
    with pytest.raises(ValueError, match="four digits"):
        Packet("Invalid year", (theme,), year=999)


def test_optional_packet_metadata_round_trips() -> None:
    packet = packet_from_data(
        {
            "name": "Metadata packet",
            "year": 2025,
            "lead_author": "Alice",
            "language": "pt-br",
            "themes": [
                {
                    "name": "Theme",
                    "author": "Alice",
                    "questions": [
                        {
                            "text": str(value),
                            "answer": "A",
                            "commentary": "",
                            "value": value,
                        }
                        for value in (10, 20, 30, 40, 50)
                    ],
                }
            ],
        }
    )

    encoded = json.loads(packet_to_json(packet))
    assert encoded["year"] == 2025
    assert encoded["lead_author"] == "Alice"
    assert encoded["language"] == "pt-BR"


def test_packet_rejects_invalid_language_tag() -> None:
    theme = Theme("Theme", (Question("Question", "Answer", "", 10),))

    with pytest.raises(ValueError, match="Language"):
        Packet("Invalid language", (theme,), language="not a language tag")


def test_docx_packet_import(tmp_path) -> None:
    paragraphs = [
        ("Heading1", "Test packet"),
        ("Heading2", "Test theme"),
        ("Normal", "Автор: Alice, Bob"),
    ]
    for value in (10, 20, 30, 40, 50):
        paragraphs.extend(
            [
                ("Normal", f"{value}) [SUBJECT]"),
                ("Normal", f"Question {value}"),
                ("Normal", ""),
                ("Normal", f"Ответ: ({value}) answer"),
                ("Normal", "Зачёт: alternative one, alternative two"),
                ("Normal", f"Комментарий: Commentary {value}"),
                ("Normal", f"Источник: https://example.com/{value}"),
            ]
        )
        if value == 10:
            paragraphs.append(("Normal", "Author: Carol"))

    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
        for style, text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    )
    path = tmp_path / "packet.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    packet = packet_from_docx(path)
    assert packet_from_docx_bytes(path.read_bytes()) == packet
    question = packet.themes[0].questions[0]

    assert packet.name == "Test packet"
    assert packet.themes[0].author == "Alice, Bob"
    assert question.author == "Carol"
    assert question.form == "SUBJECT"
    assert question.source == "https://example.com/10"
    assert question.accepted_answers == ("alternative one", "alternative two")
    assert json.loads(packet_to_json(packet))["themes"][0]["questions"][0]["value"] == 10

    json_path = tmp_path / "packet.json"
    json_path.write_text(packet_to_json(packet), encoding="utf-8")
    assert packet_from_json(json_path) == packet


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"name": "Missing themes"},
        {"name": "Bad themes", "themes": "not-a-list"},
    ],
)
def test_invalid_packet_json_is_reported_with_context(tmp_path, data: object) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="does not contain a valid packet"):
        packet_from_json(path)


def test_unreadable_docx_is_rejected(tmp_path) -> None:
    path = tmp_path / "invalid.docx"
    path.write_bytes(b"not a zip archive")

    with pytest.raises(ValueError, match="not a readable DOCX"):
        packet_from_docx(path)
