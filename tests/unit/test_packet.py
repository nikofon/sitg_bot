import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter

from sitg_bot.domain.packet import (
    SIMILAR_PACKET_QUESTION_FRACTION,
    Packet,
    Question,
    Theme,
    packet_question_fingerprints,
    similar_question_share,
)
from sitg_bot.packet_import import (
    ZeroThemesError,
    packet_from_data,
    packet_from_docx,
    packet_from_docx_bytes,
    packet_from_json,
    packet_to_json,
    packets_from_document_bytes,
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


def test_rejected_answers_round_trip_and_optional_expansion() -> None:
    question = Question(
        "Text",
        "Answer",
        "",
        10,
        accepted_answers=("Alt",),
        rejected_answers=("Near (miss) one", "Near miss"),
    )

    assert question.all_answers == ("Answer", "Alt")
    assert question.all_rejected_answers == ("Near miss one", "Near one", "Near miss")
    assert Question("Text", "Answer", "", 10).all_rejected_answers == ()

    packet = packet_from_data(
        {
            "name": "Rejected packet",
            "themes": [
                {
                    "name": "Theme",
                    "questions": [
                        {
                            "text": "Text",
                            "answer": "Answer",
                            "value": 10,
                            "rejected_answers": ["Near miss"],
                        }
                    ],
                }
            ],
        }
    )
    encoded = json.loads(packet_to_json(packet))
    assert packet.themes[0].questions[0].rejected_answers == ("Near miss",)
    assert encoded["themes"][0]["questions"][0]["rejected_answers"] == ["Near miss"]


def test_theme_accepts_ruleset_configured_values_but_requires_increasing_order() -> None:
    assert Theme("Valid", (Question("Text", "Answer", "Commentary", 100),)).questions

    with pytest.raises(ValueError, match="unique increasing non-negative"):
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


def test_zero_point_question_json_round_trip() -> None:
    packet = Packet("Zero points", (
        Theme("With zero", tuple(Question(str(v), "A", "", v) for v in (0, 10, 20))),
        Theme("Without zero", tuple(Question(str(v), "A", "", v) for v in (10, 20))),
    ))
    assert packet_from_data(json.loads(packet_to_json(packet))) == packet


@pytest.mark.parametrize("values", [(-1, 10), (0, 0, 10), (10, 0), (False, 10)])
def test_zero_point_questions_still_require_unique_increasing_integer_values(values) -> None:
    with pytest.raises(ValueError, match="unique increasing non-negative"):
        Theme("Invalid", tuple(Question("Q", "A", "", v) for v in values))


@pytest.mark.parametrize("extension", [".docx", ".pdf"])
def test_zero_point_document_question(monkeypatch, extension) -> None:
    lines = [
        "Тема: With zero", "0. [ANSWER] Warmup", "Ответ: Zero answer",
        "10. Regular question", "Ответ: Regular answer",
    ]
    if extension == ".pdf":
        page = SimpleNamespace(
            get_contents=lambda: SimpleNamespace(get_data=lambda: b"text"),
            extract_text=lambda **kwargs: "\n".join(lines),
        )
        monkeypatch.setattr("sitg_bot.packet_import.PdfReader", lambda _: SimpleNamespace(
            pages=[page], is_encrypted=False,
        ))
        source = b"pdf"
    else:
        source = docx_bytes([("Normal", line) for line in lines])
    packet, = packets_from_document_bytes(source, f"zero{extension}")
    zero, regular = packet.themes[0].questions
    assert (zero.value, zero.text, zero.answer, zero.form) == (0, "Warmup", "Zero answer", "ANSWER")
    assert regular.value == 10


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
                    "commentary": "Theme commentary",
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
    assert encoded["themes"][0]["commentary"] == "Theme commentary"
    assert packet.themes[0].commentary == "Theme commentary"


def test_packet_rejects_invalid_language_tag() -> None:
    theme = Theme("Theme", (Question("Question", "Answer", "", 10),))

    with pytest.raises(ValueError, match="Language"):
        Packet("Invalid language", (theme,), language="not a language tag")


def test_docx_packet_import(tmp_path) -> None:
    paragraphs = [
        ("Heading1", "Test packet"),
        ("Heading2", "Test theme"),
        ("Normal", "Автор: Alice, Bob"),
        ("Normal", "Комментарий к теме: Theme commentary first"),
        ("Normal", "Theme commentary continuation"),
    ]
    for value in (10, 20, 30, 40, 50):
        paragraphs.extend(
            [
                ("Normal", f"{value}) [SUBJECT]"),
                ("Normal", f"Question {value}"),
                ("Normal", ""),
                ("Normal", f"Ответ: ({value}) answer"),
                ("Normal", "Зачёт: alternative one, alternative two"),
                ("Normal", "Незачёт: near miss one, near miss two"),
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
    assert packet.themes[0].commentary == "Theme commentary first\nTheme commentary continuation"
    assert question.author == "Carol"
    assert question.form == "SUBJECT"
    assert question.source == "https://example.com/10"
    assert question.accepted_answers == ("alternative one", "alternative two")
    assert question.rejected_answers == ("near miss one", "near miss two")
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


def docx_bytes(paragraphs: list[tuple[str, str]]) -> bytes:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r><w:t>{escape(text)}</w:t></w:r></w:p>'
        for style, text in paragraphs
    )
    source = io.BytesIO()
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        )
    return source.getvalue()


def test_zero_theme_interpretations_raise_a_dedicated_error() -> None:
    with pytest.raises(ZeroThemesError, match="at least one theme"):
        packet_from_data({"name": "Empty", "themes": []})
    with pytest.raises(ZeroThemesError, match="recognizable theme headings"):
        packets_from_document_bytes(docx_bytes([("Normal", "Only preamble text")]), "p.docx")
    with pytest.raises(ZeroThemesError, match="no themes with questions"):
        packets_from_document_bytes(
            docx_bytes([("Heading2", "Theme without questions")]), "p.docx"
        )


def test_docx_multiple_packet_headings() -> None:
    source = docx_bytes([
        (style, text)
        for name in ("First", "Second")
        for style, text in [
            ("Heading1", name), ("Heading2", f"{name} theme"),
            ("Normal", "10. Question Ответ: Answer"),
        ]
    ])
    packets = packets_from_document_bytes(source, "test.docx")
    assert [p.name for p in packets] == ["First", "Second"]
    assert [p.themes[0].name for p in packets] == ["First theme", "Second theme"]
    with pytest.raises(ValueError, match="several packets"):
        packet_from_docx_bytes(source)
    single = docx_bytes([
        ("Heading1", "Named packet"), ("Heading2", "Финал"),
        ("Normal", "10. Question Ответ: Answer"),
    ])
    assert packet_from_docx_bytes(single).name == "Named packet"


@pytest.mark.parametrize("extension", [".pdf", ".docx"])
def test_document_sections_and_wrapped_fields(monkeypatch, extension) -> None:
    sections = ["БОЙ I", "Бой 2", "ПЕРВЫЙ ЭТАП", "2-й ЭТАП", "ГРАНД-ФИНАЛ", "ЗАПАС"]
    lines = ["Preamble", "10. This is not a question", "Ответ: Ignore"]
    for section in sections:
        lines.extend([
            section, "Темы:", "10. A table of contents entry",
            "1. ТЕМА: First", "Автор: Alice", "Комментарий: Theme note",
            "continued", "10. First line", "second line", "Ответ: A",
            "Источники:", "https://example.org", "50. A question about prices",
            "30. This continues the question", "Ответ: B",
            "Тема 2. SecondАвтор: Bob", "10. Other question Ответ: C",
            "Зачёт: D, E", "Тема 3.", "Автор:",
        ])
    if extension == ".pdf":
        page = SimpleNamespace(get_contents=lambda: SimpleNamespace(get_data=lambda: b"text"),
                               extract_text=Mock(
            return_value="\n".join(lines)
        ))
        monkeypatch.setattr("sitg_bot.packet_import.PdfReader", lambda _: SimpleNamespace(
            pages=[page], is_encrypted=False
        ))
        source = b"pdf"
    else:
        source = docx_bytes([("Normal", line) for line in lines])
    packets = packets_from_document_bytes(source, f"Tournament{extension}")
    assert [p.name for p in packets] == [
        f"Tournament. {name}" for name in ["Бой 1", "Бой 2", *sections[2:]]
    ]
    for packet in packets:
        assert len(packet.themes) == 2
        first, second = packet.themes
        assert first.commentary == "Theme note\ncontinued"
        assert first.questions[0].text == "First line\nsecond line"
        assert first.questions[0].source == "https://example.org"
        assert first.questions[1].text.endswith("30. This continues the question")
        assert second.name == "Second"
        assert second.questions[0].author == "Bob"
        assert second.questions[0].accepted_answers == ("D", "E")
    if extension == ".pdf":
        page.extract_text.assert_called_once_with(
            extraction_mode="layout", layout_mode_space_vertically=False
        )


def test_pdf_empty_encrypted_and_unreadable_inputs() -> None:
    with pytest.raises(ValueError, match="not a readable PDF"):
        packets_from_document_bytes(b"invalid", "test.pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    source = io.BytesIO()
    writer.write(source)
    with pytest.raises(ValueError, match="no extractable text"):
        packets_from_document_bytes(source.getvalue(), "empty.pdf")
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ValueError, match="Password-protected"):
        packets_from_document_bytes(encrypted.getvalue(), "encrypted.pdf")


def test_multi_packet_json_round_trip() -> None:
    packet = Packet("Example", (Theme("Theme", (Question("Q", "A", "", 10),)),))
    single = packet_to_json(packet)
    assert packets_from_document_bytes(single.encode(), "test.json") == (packet,)
    assert packets_from_document_bytes(f"[{single},{single}]".encode(), "test.json") == (
        packet, packet,
    )


def test_packet_similarity_ignores_metadata_but_not_content() -> None:
    original = Packet(
        "Original packet",
        (Theme("Theme", (Question("What is the answer?", "42", "", 10, author="Alice"),)),),
        year=2000,
        lead_author="Lead Author",
    )
    renamed = Packet(
        "Renamed packet",
        (Theme("Renamed theme", (Question(
            "  what   IS\tthe answer? ", " 42 ", "", 10, author="Bob",
        ),)),),
        year=2020,
        lead_author="Different Author",
        language="en",
    )
    different = Packet(
        "Different packet",
        (Theme("Theme", (Question("What is the answer?", "43", "", 10),)),),
    )

    fingerprints = packet_question_fingerprints(original)
    assert fingerprints == packet_question_fingerprints(renamed)
    assert similar_question_share(fingerprints, packet_question_fingerprints(renamed)) == 1.0
    assert similar_question_share(
        fingerprints, packet_question_fingerprints(different)
    ) == 0.0
    assert similar_question_share(frozenset(), fingerprints) == 0.0


def test_similar_question_share_threshold_and_partial_overlap() -> None:
    questions = tuple(
        Question(f"Question {value}", f"answer {value}", "", value)
        for value in (10, 20, 30, 40, 50)
    )
    fingerprints = packet_question_fingerprints(
        Packet("Draft", (Theme("Theme", questions),))
    )
    existing_questions = questions[:4] + (
        Question("Different question", "different answer", "", 50),
    )
    existing = packet_question_fingerprints(
        Packet("Existing", (Theme("Theme", existing_questions),))
    )

    share = similar_question_share(fingerprints, existing)
    assert share == 0.8
    assert share < SIMILAR_PACKET_QUESTION_FRACTION
    assert similar_question_share(
        fingerprints, packet_question_fingerprints(Packet("Same", (Theme("T", questions),)))
    ) == 1.0
