import argparse
import io
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from sitg_bot.domain.packet import Packet, Question, Theme

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": WORD_NAMESPACE}
QUESTION_RE = re.compile(r"^(10|20|30|40|50)\s*[.)]\s*(?:\[([^]]*)])?\s*(.*)$")
FIELD_RE = re.compile(
    r"^(Ответ|Зач[её]т|Комментарий|Источник|Автор(?: вопроса)?|Author)\s*:\s*(.*)$",
    re.I,
)
THEME_COMMENTARY_RE = re.compile(
    r"^(?:Комментарий к теме|Theme commentary)\s*:\s*(.*)$",
    re.I,
)
INLINE_FIELD_RE = re.compile(
    r"(?=\s+(?:Ответ|Зач[её]т|Комментарий|Источник|Автор(?: вопроса)?|Author)\s*:)",
    re.I,
)
MAX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
MAX_PACKET_THEMES = 256
MAX_PACKET_QUESTIONS = 4096


class Line(NamedTuple):
    text: str
    style: str


def _document_lines(source: str | Path | io.BytesIO) -> list[Line]:
    try:
        with ZipFile(source) as archive:
            document = archive.getinfo("word/document.xml")
            if document.file_size > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("The DOCX document content is too large")
            root = ElementTree.fromstring(archive.read(document))
    except (BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise ValueError("The uploaded document is not a readable DOCX file") from error

    lines: list[Line] = []
    for paragraph in root.findall(".//w:body/w:p", NS):
        style_node = paragraph.find("./w:pPr/w:pStyle", NS)
        style = style_node.get(f"{{{WORD_NAMESPACE}}}val", "") if style_node is not None else ""
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{{{WORD_NAMESPACE}}}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{{{WORD_NAMESPACE}}}tab":
                parts.append("\t")
            elif node.tag == f"{{{WORD_NAMESPACE}}}br":
                parts.append("\n")
        paragraph_lines = "".join(parts).split("\n")
        for text in paragraph_lines:
            chunks = INLINE_FIELD_RE.split(text)
            lines.extend(Line(chunk.strip(), style) for chunk in chunks)
    return lines


def _heading_level(style: str) -> int | None:
    normalized = style.casefold().replace(" ", "")
    if normalized in {"heading1", "заголовок1"}:
        return 1
    if normalized in {"heading2", "заголовок2"}:
        return 2
    return None


def packet_from_docx(path: str | Path) -> Packet:
    """Parse a marked-up DOCX question packet into the domain model."""
    return _packet_from_docx_source(Path(path))


def packet_from_docx_bytes(source: bytes) -> Packet:
    """Parse DOCX bytes without retaining an original source file on the server."""
    return _packet_from_docx_source(io.BytesIO(source))


def _packet_from_docx_source(source: str | Path | io.BytesIO) -> Packet:
    """Internal DOCX parser shared by filesystem and transport inputs."""
    packet_name = ""
    themes: list[Theme] = []
    theme_name = ""
    theme_author = ""
    theme_commentary = ""
    questions: list[Question] = []
    question: dict[str, object] | None = None
    active_field = "text"

    def append(field: str, value: str) -> None:
        if question is None or not value:
            return
        current = str(question[field])
        question[field] = f"{current}\n{value}" if current else value

    def finish_question() -> None:
        nonlocal question
        if question is None:
            return
        value = int(question["value"])
        if not str(question["text"]).strip():
            raise ValueError(f"Question {value} in theme {theme_name!r} has no text")
        if not str(question["answer"]).strip():
            raise ValueError(f"Question {value} in theme {theme_name!r} has no answer")
        accepted = tuple(
            answer.strip()
            for answer in str(question["accepted_answers"]).split(",")
            if answer.strip()
        )
        questions.append(
            Question(
                text=str(question["text"]).strip(),
                answer=str(question["answer"]).strip(),
                commentary=str(question["commentary"]).strip(),
                value=value,
                accepted_answers=accepted,
                form=str(question["form"]).strip(),
                source=str(question["source"]).strip(),
                author=str(question["author"]).strip() or theme_author,
            )
        )
        question = None

    def finish_theme() -> None:
        nonlocal questions, theme_commentary
        finish_question()
        if theme_name:
            themes.append(
                Theme(theme_name, tuple(questions), theme_author, theme_commentary.strip())
            )
        questions = []
        theme_commentary = ""

    for line in _document_lines(source):
        heading = _heading_level(line.style)
        if heading == 1 and line.text:
            if packet_name:
                raise ValueError("A DOCX packet must contain exactly one level-1 heading")
            packet_name = line.text
            continue
        if heading == 2:
            if not line.text:
                continue
            finish_theme()
            theme_name = line.text
            theme_author = ""
            continue
        theme_commentary_match = THEME_COMMENTARY_RE.match(line.text)
        if theme_commentary_match:
            finish_question()
            value = theme_commentary_match.group(1).strip()
            theme_commentary = f"{theme_commentary}\n{value}" if theme_commentary else value
            continue
        marker = QUESTION_RE.match(line.text)
        if marker:
            if not theme_name:
                raise ValueError(f"Question {marker.group(1)} appears before a theme heading")
            finish_question()
            value, form, inline_text = marker.groups()
            question = {
                "value": int(value), "form": form or "", "text": inline_text,
                "answer": "", "accepted_answers": "", "commentary": "",
                "source": "", "author": "",
            }
            active_field = "text"
            continue
        field_match = FIELD_RE.match(line.text)
        if field_match:
            label, value = field_match.groups()
            label = label.casefold().replace("ё", "е")
            if (label.startswith("автор") or label == "author") and question is None:
                theme_author = value.strip()
                continue
            if question is None:
                continue
            active_field = {
                "ответ": "answer", "зачет": "accepted_answers",
                "комментарий": "commentary", "источник": "source",
                "автор": "author", "автор вопроса": "author", "author": "author",
            }[label]
            append(active_field, value.strip())
            continue
        if line.text and question is not None:
            append(active_field, line.text)
        elif line.text and question is None and theme_commentary:
            theme_commentary = f"{theme_commentary}\n{line.text}"
    finish_theme()
    if not packet_name:
        raise ValueError("A DOCX packet must have a non-empty level-1 heading")
    if not themes:
        raise ValueError("A DOCX packet must contain at least one level-2 theme heading")
    return Packet(packet_name, tuple(themes))


def packet_to_json(packet: Packet, *, indent: int | None = 2) -> str:
    return json.dumps(asdict(packet), ensure_ascii=False, indent=indent)


def packet_from_data(data: Mapping[str, Any]) -> Packet:
    """Build and validate a packet from its JSON-compatible representation."""
    def string(value: object, field: str, *, default: str | None = None) -> str:
        if value is None and default is not None:
            return default
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        return value

    def sequence(value: object, field: str) -> list[Any] | tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{field} must be an array")
        return value

    try:
        themes: list[Theme] = []
        raw_themes = sequence(data["themes"], "themes")
        if len(raw_themes) > MAX_PACKET_THEMES:
            raise ValueError(f"a packet may contain at most {MAX_PACKET_THEMES} themes")
        question_count = 0
        for theme_index, raw_theme in enumerate(raw_themes, 1):
            if not isinstance(raw_theme, Mapping):
                raise TypeError(f"theme {theme_index} must be an object")
            questions: list[Question] = []
            raw_questions = sequence(
                raw_theme["questions"], f"theme {theme_index} questions"
            )
            question_count += len(raw_questions)
            if question_count > MAX_PACKET_QUESTIONS:
                raise ValueError(
                    f"a packet may contain at most {MAX_PACKET_QUESTIONS} questions"
                )
            for question_index, raw_question in enumerate(raw_questions, 1):
                if not isinstance(raw_question, Mapping):
                    raise TypeError(
                        f"theme {theme_index} question {question_index} must be an object"
                    )
                accepted = sequence(
                    raw_question.get("accepted_answers", ()),
                    f"theme {theme_index} question {question_index} accepted_answers",
                )
                if not all(isinstance(answer, str) for answer in accepted):
                    raise TypeError("accepted_answers entries must be strings")
                value = raw_question["value"]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(
                        f"theme {theme_index} question {question_index} value must be an integer"
                    )
                questions.append(
                    Question(
                        text=string(raw_question["text"], "question text"),
                        answer=string(raw_question["answer"], "question answer"),
                        commentary=string(
                            raw_question.get("commentary", ""), "question commentary"
                        ),
                        value=value,
                        accepted_answers=tuple(accepted),
                        form=string(raw_question.get("form", ""), "question form"),
                        source=string(raw_question.get("source", ""), "question source"),
                        author=string(raw_question.get("author", ""), "question author"),
                    )
                )
            themes.append(
                Theme(
                    name=string(raw_theme["name"], "theme name"),
                    author=string(raw_theme.get("author", ""), "theme author"),
                    commentary=string(raw_theme.get("commentary", ""), "theme commentary"),
                    questions=tuple(questions),
                )
            )
        return Packet(
            name=string(data["name"], "packet name"),
            themes=tuple(themes),
            year=(int(data["year"]) if data.get("year") not in (None, "") else None),
            lead_author=string(data.get("lead_author"), "lead_author", default=""),
            language=string(data.get("language"), "language", default="und"),
        )
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"Packet data is invalid: {error}") from error


def packet_from_json(path: str | Path) -> Packet:
    """Load a packet from its JSON representation."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("the document root must be an object")
        return packet_from_data(data)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        raise ValueError(f"{path} does not contain a valid packet: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert a marked-up DOCX packet to JSON")
    parser.add_argument("input", type=Path, help="path to the source .docx file")
    parser.add_argument("output", type=Path, nargs="?", help="output path (stdout by default)")
    args = parser.parse_args()
    rendered = packet_to_json(packet_from_docx(args.input)) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
