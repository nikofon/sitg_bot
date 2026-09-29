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

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from sitg_bot.domain.packet import Packet, Question, Theme

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": WORD_NAMESPACE}
QUESTION_RE = re.compile(r"^(10|20|30|40|50)\s*[.)]\s*(?:\[([^]]*)])?\s*(.*)$")
FIELD_RE = re.compile(
    r"^(Ответ|Незач[её]т|Зач[её]т|Комментарий|Источники?|Автор(?: вопроса)?|Author)\s*:\s*(.*)$",
    re.I,
)
THEME_COMMENTARY_RE = re.compile(
    r"^(?:Комментарий к теме|Theme commentary)\s*:\s*(.*)$",
    re.I,
)
INLINE_FIELD_RE = re.compile(
    r"(?=\s+(?:Ответ|Незач[её]т|Зач[её]т|Комментарий|Источники?|Автор(?: вопроса)?|Author)\s*:)",
    re.I,
)
MAX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
MAX_PACKET_THEMES = 256
MAX_PACKET_QUESTIONS = 4096
MAX_DOCUMENT_PACKETS = 128
MAX_PDF_PAGES = 512
SECTION_RE = re.compile(
    r"^(?:Бой\s+(?:\d+|[IVXLCDM]+)|"
    r"(?:\d+(?:[-‐‑–]?[йя])?|[IVXLCDM]+|Первый|Второй|Третий|Четв[её]ртый|"
    r"Пятый|Шестой|Седьмой|Восьмой|Девятый|Десятый)\s+этап|"
    r"Этап\s+(?:\d+|[IVXLCDM]+)|Гранд[\s\-‐‑–—]*финал|Финал|Запас)\s*[.:]?$",
    re.I,
)
THEME_RE = re.compile(
    r"^(?:\d+\s*[.)]\s*Тема\s*:|Тема\s+(?:№\s*)?\d+\s*[.):]|Тема\s*:)\s*(.*)$",
    re.I,
)


class ZeroThemesError(ValueError):
    """The source was read, but interpreted as containing no themes with questions."""


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
    return _single_packet(packets_from_document_bytes(Path(path).read_bytes(), Path(path).name))


def packet_from_docx_bytes(source: bytes) -> Packet:
    """Parse DOCX bytes without retaining an original source file on the server."""
    return _single_packet(packets_from_document_bytes(source, "packet.docx"))


def _single_packet(packets: tuple[Packet, ...]) -> Packet:
    if len(packets) != 1:
        raise ValueError("The document contains several packets; use packets_from_document_bytes")
    return packets[0]


def _pdf_lines(source: bytes) -> list[Line]:
    try:
        reader = PdfReader(io.BytesIO(source))
        if reader.is_encrypted:
            raise ValueError("Password-protected PDF files are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("The PDF has too many pages")
        lines: list[Line] = []
        size = 0
        for page in reader.pages:
            content = page.get_contents()
            if content is None:
                continue
            if len(content.get_data()) > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("The PDF page content is too large")
            text = page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False)
            size += len(text.encode("utf-8"))
            if size > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("The PDF text content is too large")
            # Page numbers are not question content. Keep line breaks across pages.
            page_lines = text.strip().splitlines()
            lines.extend(Line(line.strip(), "") for index, line in enumerate(page_lines)
                         if not (index in {0, len(page_lines) - 1}
                                 and line.strip().isdecimal()))
    except (PyPdfError, OSError, RecursionError) as error:
        raise ValueError("The uploaded document is not a readable PDF file") from error
    if not any(line.text for line in lines):
        raise ValueError("The PDF has no extractable text; scanned files require OCR")
    return lines


def _section_name(text: str) -> str:
    text = text.rstrip(".:").strip()
    if text.casefold().startswith("бой"):
        number = text.split()[1].upper()
        if not number.isdecimal():
            values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
            number = str(sum(-values[c] if i + 1 < len(number)
                             and values[c] < values[number[i + 1]] else values[c]
                             for i, c in enumerate(number)))
        return f"Бой {number}"
    return text


def packets_from_document_bytes(source: bytes, source_filename: str) -> tuple[Packet, ...]:
    """Parse one upload into packets, preserving document order."""
    path = Path(source_filename)
    extension = path.suffix.casefold()
    if extension == ".json":
        data = json.loads(source.decode("utf-8-sig"))
        entries = data if isinstance(data, list) else [data]
        if not entries or len(entries) > MAX_DOCUMENT_PACKETS:
            raise ValueError(f"A document must contain 1–{MAX_DOCUMENT_PACKETS} packets")
        if not all(isinstance(entry, dict) for entry in entries):
            raise ValueError("Packet JSON must be an object or an array of packet objects")
        return tuple(packet_from_data(entry) for entry in entries)
    if extension == ".docx":
        lines = _document_lines(io.BytesIO(source))
    elif extension == ".pdf":
        lines = _pdf_lines(source)
    else:
        raise ValueError("Only DOCX, PDF and JSON packet files are supported")

    sections = any(_heading_level(line.style) != 2 and SECTION_RE.fullmatch(line.text)
                   for line in lines)
    packets: list[Packet] = []
    name = path.stem
    body: list[Line] = []
    started = not sections

    def finish() -> None:
        if not any(_heading_level(line.style) == 2 or THEME_RE.match(line.text)
                   for line in body):
            return  # Document title, preamble or table of contents.
        if len(packets) >= MAX_DOCUMENT_PACKETS:
            raise ValueError(f"A document may contain at most {MAX_DOCUMENT_PACKETS} packets")
        packets.append(_packet_from_lines(body, name))

    for line in lines:
        section = SECTION_RE.fullmatch(line.text) if _heading_level(line.style) != 2 else None
        if section or (not sections and _heading_level(line.style) == 1 and line.text):
            finish()
            body = []
            name = f"{path.stem}. {_section_name(line.text)}" if section else line.text
            started = True
        elif started:
            body.append(line)
    finish()
    if not packets:
        raise ZeroThemesError("The document contains no packets with recognizable theme headings")
    return tuple(packets)


def _packet_from_lines(lines: list[Line], packet_name: str) -> Packet:
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
        rejected = tuple(
            answer.strip()
            for answer in str(question["rejected_answers"]).split(",")
            if answer.strip()
        )
        questions.append(
            Question(
                text=str(question["text"]).strip(),
                answer=str(question["answer"]).strip(),
                commentary=str(question["commentary"]).strip(),
                value=value,
                accepted_answers=accepted,
                rejected_answers=rejected,
                form=str(question["form"]).strip(),
                source=str(question["source"]).strip(),
                author=str(question["author"]).strip() or theme_author,
            )
        )
        question = None

    def finish_theme() -> None:
        nonlocal questions, theme_commentary
        finish_question()
        if theme_name and questions:
            themes.append(
                Theme(theme_name, tuple(questions), theme_author, theme_commentary.strip())
            )
        questions = []
        theme_commentary = ""

    normalized: list[Line] = []
    for line in lines:
        text = line.text
        if THEME_RE.match(text):
            text = re.sub(r"(?<!\s)(?=Автор\s*:)", "\n", text, flags=re.I)
        for part in text.splitlines():
            normalized.extend(Line(chunk.strip(), line.style)
                              for chunk in INLINE_FIELD_RE.split(part))

    for line in normalized:
        heading = _heading_level(line.style)
        theme_marker = THEME_RE.match(line.text)
        if heading == 2 or theme_marker:
            if not line.text:
                continue
            finish_theme()
            theme_name = theme_marker.group(1).strip() if theme_marker else line.text
            theme_author = ""
            continue
        if not theme_name:
            continue  # Ignore preambles and numbered theme indexes before the first theme.
        theme_commentary_match = THEME_COMMENTARY_RE.match(line.text)
        if theme_commentary_match or (
            question is None and theme_name and re.match(r"^Комментарий\s*:", line.text, re.I)
        ):
            finish_question()
            value = line.text.split(":", 1)[1].strip()
            theme_commentary = f"{theme_commentary}\n{value}" if theme_commentary else value
            continue
        marker = QUESTION_RE.match(line.text)
        if (marker and question is not None and not question["answer"]
                and int(marker.group(1)) <= int(question["value"])):
            marker = None  # A wrapped question can mention a lower price followed by a period.
        if marker:
            finish_question()
            value, form, inline_text = marker.groups()
            question = {
                "value": int(value), "form": form or "", "text": inline_text,
                "answer": "", "accepted_answers": "", "rejected_answers": "",
                "commentary": "", "source": "", "author": "",
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
                "ответ": "answer", "зачет": "accepted_answers", "незачет": "rejected_answers",
                "комментарий": "commentary", "источник": "source", "источники": "source",
                "автор": "author", "автор вопроса": "author", "author": "author",
            }[label]
            append(active_field, value.strip())
            continue
        if line.text and question is not None:
            append(active_field, line.text)
        elif line.text and question is None and theme_commentary:
            theme_commentary = f"{theme_commentary}\n{line.text}"
    finish_theme()
    if not themes:
        raise ZeroThemesError(f"Packet {packet_name!r} contains no themes with questions")
    return packet_from_data(asdict(Packet(packet_name, tuple(themes))))


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
        if not raw_themes:
            raise ZeroThemesError("A packet must contain at least one theme")
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
                rejected = sequence(
                    raw_question.get("rejected_answers", ()),
                    f"theme {theme_index} question {question_index} rejected_answers",
                )
                if not all(isinstance(answer, str) for answer in rejected):
                    raise TypeError("rejected_answers entries must be strings")
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
                        rejected_answers=tuple(rejected),
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
    except ZeroThemesError:
        raise
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
    parser = argparse.ArgumentParser(description="Convert DOCX/PDF packets to JSON")
    parser.add_argument("input", type=Path, help="path to the source .docx, .pdf or .json file")
    parser.add_argument("output", type=Path, nargs="?", help="output path (stdout by default)")
    args = parser.parse_args()
    packets = packets_from_document_bytes(args.input.read_bytes(), args.input.name)
    data = asdict(packets[0]) if len(packets) == 1 else [asdict(packet) for packet in packets]
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
