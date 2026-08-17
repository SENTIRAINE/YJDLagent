from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app.rag.models import Chunk, SourceDocument


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    page_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    section_path TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    warnings TEXT NOT NULL,
    embedding BLOB NOT NULL,
    UNIQUE(document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_content_type ON chunks(content_type);
"""


class SQLiteVectorStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def rebuild(
        self,
        documents: list[SourceDocument],
        chunks: list[Chunk],
        embeddings: np.ndarray,
        provider_name: str,
        dimension: int,
    ) -> None:
        if embeddings.shape != (len(chunks), dimension):
            raise ValueError("Embedding matrix shape does not match chunks and dimension")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            with connection:
                connection.execute("DELETE FROM chunks")
                connection.execute("DELETE FROM documents")
                connection.execute("DELETE FROM metadata")
                connection.executemany(
                    """
                    INSERT INTO documents(document_id, title, author, source, sha256, page_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document.document_id,
                            document.title,
                            document.author,
                            document.path.name,
                            document.sha256,
                            document.page_count,
                        )
                        for document in documents
                    ],
                )
                rows = []
                for chunk, vector in zip(chunks, embeddings, strict=True):
                    rows.append(
                        (
                            chunk.chunk_id,
                            chunk.document_id,
                            chunk.ordinal,
                            chunk.content,
                            json.dumps(chunk.section_path, ensure_ascii=False),
                            chunk.page_start,
                            chunk.page_end,
                            chunk.content_type,
                            chunk.source,
                            chunk.title,
                            json.dumps(chunk.warnings, ensure_ascii=False),
                            np.asarray(vector, dtype=np.float32).tobytes(),
                        )
                    )
                connection.executemany(
                    """
                    INSERT INTO chunks(
                        chunk_id, document_id, ordinal, content, section_path, page_start,
                        page_end, content_type, source, title, warnings, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                metadata = {
                    "schema_version": "1",
                    "embedding_provider": provider_name,
                    "embedding_dimension": str(dimension),
                    "document_count": str(len(documents)),
                    "chunk_count": str(len(chunks)),
                    "built_at": datetime.now(UTC).isoformat(),
                }
                connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items())

    def metadata(self) -> dict[str, str]:
        if not self.path.exists():
            raise FileNotFoundError(f"RAG index not found: {self.path}")
        with closing(self._connect()) as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM metadata")}

    def load_chunks(self) -> list[tuple[Chunk, np.ndarray]]:
        metadata = self.metadata()
        dimension = int(metadata["embedding_dimension"])
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM chunks ORDER BY document_id, ordinal").fetchall()
        result: list[tuple[Chunk, np.ndarray]] = []
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            if vector.size != dimension:
                raise ValueError(f"Invalid vector dimension for chunk {row['chunk_id']}")
            result.append(
                (
                    Chunk(
                        chunk_id=row["chunk_id"],
                        document_id=row["document_id"],
                        ordinal=row["ordinal"],
                        content=row["content"],
                        section_path=json.loads(row["section_path"]),
                        page_start=row["page_start"],
                        page_end=row["page_end"],
                        content_type=row["content_type"],
                        source=row["source"],
                        title=row["title"],
                        warnings=json.loads(row["warnings"]),
                    ),
                    vector.copy(),
                )
            )
        return result
