from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Iterable

import pdfplumber
from pypdf import PdfReader

from app.rag.models import ExtractedTable, PageData, SourceDocument


HEADER_PREFIX = "步行指数知识库 · 大连市社区生活圈案例"
PAGE_NUMBER_RE = re.compile(r"^第\s*\d+\s*页$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repair_formula_layout(lines: list[str], page_number: int) -> tuple[list[str], list[str]]:
    repaired: list[str] = []
    warnings: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if (
            page_number == 5
            and index + 2 < len(lines)
            and line == "n"
            and lines[index + 1].strip().startswith("S = ∑ W")
            and lines[index + 2].strip().startswith("a b=1 b")
        ):
            repaired.append("步行指数公式：S_a = ∑_{b=1}^{n} W_b × (1 - α) × (1 - β)")
            warnings.append("formula_layout_repaired_from_visual_source")
            index += 3
            continue
        if (
            page_number == 6
            and index + 1 < len(lines)
            and line.startswith("C = S / S × 100%")
            and lines[index + 1].strip() == "A B"
        ):
            repaired.append("基础设施覆盖率公式：C = S_A / S_B × 100%")
            warnings.append("formula_layout_repaired_from_visual_source")
            index += 2
            continue
        repaired.append(lines[index])
        index += 1
    return repaired, warnings


def clean_page_text(text: str, page_number: int) -> tuple[str, list[str]]:
    text = unicodedata.normalize("NFC", text or "")
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line or line == HEADER_PREFIX or PAGE_NUMBER_RE.fullmatch(line):
            continue
        lines.append(line)

    lines, warnings = _repair_formula_layout(lines, page_number)
    text = "\n".join(lines)

    if page_number == 5:
        text = text.replace("S\x00", "S_a").replace("W\x00", "W_b")
    elif page_number == 6:
        text = text.replace("S\x00", "S_A", 1).replace("S\x00", "S_B", 1)
    elif "\x00" in text:
        warnings.append("unresolved_null_character_from_pdf")

    text = text.replace("\x00", "")
    return text.strip(), sorted(set(warnings))


def _clean_cell(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("|", "\\|")


def table_to_markdown(table: Iterable[Iterable[object]]) -> tuple[str, int, int]:
    rows = [[_clean_cell(cell) for cell in row] for row in table]
    rows = [row for row in rows if any(row)]
    if not rows:
        return "", 0, 0
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    body = rows[1:]
    markdown = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    markdown.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(markdown), len(rows), width


def extract_document(path: Path) -> SourceDocument:
    path = path.resolve()
    digest = sha256_file(path)
    reader = PdfReader(str(path))
    metadata = reader.metadata or {}
    title = str(metadata.get("/Title") or path.stem)
    author = str(metadata.get("/Author") or "")

    pages: list[PageData] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            raw_text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            text, warnings = clean_page_text(raw_text, page_number)
            tables: list[ExtractedTable] = []
            for raw_table in page.extract_tables() or []:
                markdown, row_count, column_count = table_to_markdown(raw_table)
                if markdown and row_count >= 2 and column_count >= 2:
                    tables.append(
                        ExtractedTable(
                            page_number=page_number,
                            markdown=markdown,
                            row_count=row_count,
                            column_count=column_count,
                        )
                    )
            pages.append(PageData(page_number=page_number, text=text, tables=tables, warnings=warnings))

    document_id = f"doc-{digest[:16]}"
    return SourceDocument(
        document_id=document_id,
        path=path,
        title=title,
        author=author,
        page_count=len(pages),
        sha256=digest,
        pages=pages,
    )
