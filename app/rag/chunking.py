from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.rag.models import Chunk, SourceDocument


HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")
TOP_LEVEL_HINTS = (
    "研究口径",
    "步行指数的含义",
    "步行指数计算",
    "基础设施覆盖率",
    "步行指数评价",
    "来源与核验",
)
UNCERTAINTY_MARKERS = (
    "原文疑点",
    "原文完整性说明",
    "原文数字口径",
    "原文方法口径",
    "原文如此",
    "原文写",
    "不自行补写",
    "不擅自修正",
)


@dataclass
class SectionBuffer:
    path: list[str]
    page_start: int
    page_end: int
    lines: list[str] = field(default_factory=list)


def _is_heading(line: str) -> tuple[int, str] | None:
    match = HEADING_RE.fullmatch(line.strip())
    if not match:
        return None
    number, title = match.groups()
    if len(title) > 45:
        return None
    if "." not in number and not any(hint in title for hint in TOP_LEVEL_HINTS):
        return None
    level = number.count(".") + 1
    return level, f"{number} {title}"


def _split_content(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    parts: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        additional = len(line) + (1 if current else 0)
        if current and current_size + additional > max_chars:
            part = "\n".join(current).strip()
            parts.append(part)
            overlap = part[-overlap_chars:] if overlap_chars else ""
            current = [overlap, line] if overlap else [line]
            current_size = sum(len(item) for item in current) + len(current) - 1
        else:
            current.append(line)
            current_size += additional
    if current:
        parts.append("\n".join(current).strip())
    return parts


def _content_type(content: str) -> str:
    if "公式:" in content or "公式：" in content or "∑" in content:
        return "formula"
    if any(marker in content for marker in UNCERTAINTY_MARKERS):
        return "warning"
    return "narrative"


def _chunk_id(document_id: str, ordinal: int, content: str) -> str:
    value = f"{document_id}\n{ordinal}\n{content}".encode("utf-8")
    return f"chk-{hashlib.sha256(value).hexdigest()[:20]}"


def _table_section_path(page_number: int, markdown: str, fallback: list[str]) -> list[str]:
    rules = (
        ("步骤 | 原文方法", ["3 步行指数计算过程与中间数据", "3.1 数据处理链"]),
        ("交叉口密度区间", ["3 步行指数计算过程与中间数据", "3.2 交叉口密度衰减率"]),
        ("原文顺序 | 街道平均长度区间", ["3 步行指数计算过程与中间数据", "3.3 街道长度衰减率"]),
        ("基础设施类别 | 权重", ["1 研究口径与数据基础", "1.3 基础设施权重"]),
        ("设施 | 平均覆盖率", ["4 基础设施覆盖率", "4.2 六类设施覆盖率数据"]),
        ("覆盖率评分区间", ["4 基础设施覆盖率", "4.3 综合覆盖率评分"]),
    )
    for marker, path in rules:
        if marker in markdown:
            return path
    if "评分区间 | 评价类别" in markdown:
        if page_number == 5:
            return ["3 步行指数计算过程与中间数据", "3.4 步行指数公式与评分"]
        if page_number == 8:
            return ["5 步行指数评价结果"]
    if "符号 | 原文含义" in markdown:
        if page_number == 5:
            return ["3 步行指数计算过程与中间数据", "3.4 步行指数公式与评分"]
        if page_number == 6:
            return ["4 基础设施覆盖率", "4.1 含义、公式与计算流程"]
    return fallback


def chunk_document(document: SourceDocument, max_chars: int = 1000, overlap_chars: int = 120) -> list[Chunk]:
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than max_chars")

    sections: list[SectionBuffer] = []
    current = SectionBuffer(path=[document.title], page_start=1, page_end=1)
    hierarchy: list[str] = []
    page_paths: dict[int, list[str]] = {}

    for page in document.pages:
        for line in page.text.splitlines():
            heading = _is_heading(line)
            if heading:
                if current.lines:
                    sections.append(current)
                level, label = heading
                hierarchy = hierarchy[: level - 1]
                hierarchy.append(label)
                current = SectionBuffer(path=hierarchy.copy(), page_start=page.page_number, page_end=page.page_number)
                continue
            current.lines.append(line)
            current.page_end = page.page_number
        page_paths[page.page_number] = hierarchy.copy() or [document.title]
    if current.lines:
        sections.append(current)

    chunks: list[Chunk] = []
    ordinal = 0
    for section in sections:
        raw_content = "\n".join(section.lines).strip()
        for part in _split_content(raw_content, max_chars=max_chars, overlap_chars=overlap_chars):
            heading_prefix = " > ".join(section.path)
            content = f"章节：{heading_prefix}\n{part}" if heading_prefix else part
            warnings = ["source_uncertainty"] if any(marker in content for marker in UNCERTAINTY_MARKERS) else []
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(document.document_id, ordinal, content),
                    document_id=document.document_id,
                    ordinal=ordinal,
                    content=content,
                    section_path=section.path,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    content_type=_content_type(content),
                    source=document.path.name,
                    title=document.title,
                    warnings=warnings,
                )
            )
            ordinal += 1

    for page in document.pages:
        for table_index, table in enumerate(page.tables, start=1):
            section_path = _table_section_path(
                page.page_number,
                table.markdown,
                page_paths.get(page.page_number, [document.title]),
            )
            content = f"章节：{' > '.join(section_path)}\n表格（第{page.page_number}页，第{table_index}个）：\n{table.markdown}"
            warnings = ["source_uncertainty"] if any(marker in content for marker in UNCERTAINTY_MARKERS) else []
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(document.document_id, ordinal, content),
                    document_id=document.document_id,
                    ordinal=ordinal,
                    content=content,
                    section_path=section_path,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    content_type="table",
                    source=document.path.name,
                    title=document.title,
                    warnings=warnings,
                )
            )
            ordinal += 1

    return chunks
