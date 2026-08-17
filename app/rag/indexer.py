from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.config import PROJECT_ROOT, Settings
from app.rag.chunking import chunk_document
from app.rag.cleaning import extract_document
from app.rag.embeddings import create_embedding_provider
from app.rag.models import Chunk
from app.rag.store import SQLiteVectorStore


def _embed_in_batches(provider: Any, chunks: list[Chunk], batch_size: int = 32) -> np.ndarray:
    batches = []
    for start in range(0, len(chunks), batch_size):
        texts = [chunk.content for chunk in chunks[start : start + batch_size]]
        batches.append(provider.embed(texts))
    if not batches:
        return np.empty((0, provider.dimension), dtype=np.float32)
    return np.vstack(batches).astype(np.float32)


def _write_processed(path: Path, chunks: list[Chunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")


def build_index(settings: Settings | None = None, source_paths: list[Path] | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    paths = source_paths or sorted(PROJECT_ROOT.glob(settings.source_glob))
    paths = [path for path in paths if path.is_file() and path.suffix.lower() == ".pdf"]
    if not paths:
        raise FileNotFoundError(f"No PDF files matched {settings.source_glob!r} under {PROJECT_ROOT}")

    documents = [extract_document(path) for path in paths]
    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    if not chunks:
        raise RuntimeError("PDF extraction produced no chunks")

    provider = create_embedding_provider(settings)
    embeddings = _embed_in_batches(provider, chunks)
    store = SQLiteVectorStore(settings.database_path)
    store.rebuild(documents, chunks, embeddings, provider.name, provider.dimension)
    _write_processed(settings.processed_path, chunks)

    manifest = {
        "documents": [
            {
                "documentId": document.document_id,
                "title": document.title,
                "source": document.path.name,
                "sha256": document.sha256,
                "pageCount": document.page_count,
            }
            for document in documents
        ],
        "documentCount": len(documents),
        "chunkCount": len(chunks),
        "contentTypes": {
            kind: sum(1 for chunk in chunks if chunk.content_type == kind)
            for kind in sorted({chunk.content_type for chunk in chunks})
        },
        "warningChunkCount": sum(1 for chunk in chunks if chunk.warnings),
        "embeddingProvider": provider.name,
        "embeddingDimension": provider.dimension,
        "databasePath": str(settings.database_path),
        "processedPath": str(settings.processed_path),
    }
    manifest_path = settings.processed_path.with_name("manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest

