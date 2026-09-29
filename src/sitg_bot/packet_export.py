"""Generate DOCX documents from the ruleset's library pages."""

import io
import re
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def library_docx(name: str, pages: list[dict[str, object]]) -> bytes:
    document = Element(f"{{{WORD}}}document")
    body = SubElement(document, f"{{{WORD}}}body")

    def paragraph(value: object, heading: int = 0) -> None:
        node = SubElement(body, f"{{{WORD}}}p")
        if heading:
            properties = SubElement(node, f"{{{WORD}}}pPr")
            SubElement(properties, f"{{{WORD}}}pStyle", {f"{{{WORD}}}val": f"Heading{heading}"})
        for index, line in enumerate(str(value).split("\n")):
            run = SubElement(node, f"{{{WORD}}}r")
            if index:
                SubElement(run, f"{{{WORD}}}br")
            text = SubElement(
                run, f"{{{WORD}}}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
            )
            text.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", line)

    paragraph(name, 1)
    for page in pages:
        paragraph(page["title"], 2)
        if page.get("author"):
            paragraph(f"Author: {page['author']}")
        for question in page["questions"]:
            paragraph(f"{question['value']}) {question['text']}")
            paragraph(f"Answer: {question['answer']}")
            for field, label in (
                ("accepted_answers", "Additional answers"),
                ("rejected_answers", "Unaccepted answers"),
                ("commentary", "Commentary"),
                ("author", "Authors"), ("form", "Form"), ("source", "Source"),
            ):
                value = question.get(field)
                if value:
                    rendered = ", ".join(value) if isinstance(value, (list, tuple)) else value
                    paragraph(f"{label}: {rendered}")
    styles = Element(f"{{{WORD}}}styles")
    for level, size in ((1, 32), (2, 28)):
        style = SubElement(styles, f"{{{WORD}}}style", {
            f"{{{WORD}}}type": "paragraph", f"{{{WORD}}}styleId": f"Heading{level}",
        })
        SubElement(style, f"{{{WORD}}}name", {f"{{{WORD}}}val": f"heading {level}"})
        properties = SubElement(style, f"{{{WORD}}}rPr")
        SubElement(properties, f"{{{WORD}}}b")
        SubElement(properties, f"{{{WORD}}}sz", {f"{{{WORD}}}val": str(size)})
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
 Target="word/document.xml"/>
</Relationships>''')
        archive.writestr("word/_rels/document.xml.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
 Target="styles.xml"/>
</Relationships>''')
        archive.writestr(
            "word/document.xml", tostring(document, encoding="utf-8", xml_declaration=True)
        )
        archive.writestr(
            "word/styles.xml", tostring(styles, encoding="utf-8", xml_declaration=True)
        )
    return buffer.getvalue()
