from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExtractedTable:
    page_number: int
    markdown: str
    row_count: int
    column_count: int


@dataclass
class PageData:
    page_number: int
    text: str
    tables: list[ExtractedTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourceDocument:
    document_id: str
    path: Path
    title: str
    author: str
    page_count: int
    sha256: str
    pages: list[PageData]


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    ordinal: int
    content: str
    section_path: list[str]
    page_start: int
    page_end: int
    content_type: str
    source: str
    title: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    chunk: Chunk
    score: float
    dense_score: float
    lexical_score: float
    number_score: float
    intent_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = self.chunk.to_dict()
        data.update(
            {
                "score": round(self.score, 6),
                "dense_score": round(self.dense_score, 6),
                "lexical_score": round(self.lexical_score, 6),
                "number_score": round(self.number_score, 6),
                "intent_score": round(self.intent_score, 6),
                "citation": self.citation,
            }
        )
        return data

    def to_api_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        aliases = {
            "chunk_id": "chunkId",
            "document_id": "documentId",
            "section_path": "sectionPath",
            "page_start": "pageStart",
            "page_end": "pageEnd",
            "content_type": "contentType",
            "dense_score": "denseScore",
            "lexical_score": "lexicalScore",
            "number_score": "numberScore",
            "intent_score": "intentScore",
        }
        return {aliases.get(key, key): value for key, value in data.items()}

    @property
    def citation(self) -> str:
        pages = str(self.chunk.page_start)
        if self.chunk.page_end != self.chunk.page_start:
            pages = f"{pages}-{self.chunk.page_end}"
        section = " > ".join(self.chunk.section_path) or "未分节"
        return f"{self.chunk.title}，第{pages}页，{section}"
