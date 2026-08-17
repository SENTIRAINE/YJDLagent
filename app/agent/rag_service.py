from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.rag.models import SearchResult
from app.rag.retriever import HybridRetriever


class RagEvidenceService:
    def __init__(self, settings: Settings, retriever: HybridRetriever | None = None):
        self.settings = settings
        self.retriever = retriever or HybridRetriever(settings)
        self._versions = self._load_document_versions(settings.processed_path.parent / "manifest.json")

    @staticmethod
    def _load_document_versions(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return {
            document["documentId"]: f"sha256:{document['sha256']}"
            for document in manifest.get("documents", [])
            if document.get("documentId") and document.get("sha256")
        }

    def search(
        self,
        query: str,
        *,
        top_k: int,
        document_ids: list[str] | None = None,
        content_types: list[str] | None = None,
    ) -> list[SearchResult]:
        return self.retriever.search(
            query,
            top_k=top_k,
            document_ids=document_ids,
            content_types=content_types,
        )

    def document_version(self, document_id: str) -> str:
        version = self._versions.get(document_id)
        if version is None:
            raise ValueError(f"document version is unavailable for {document_id}")
        return version

    def resource_ref(self, result: SearchResult) -> str:
        version = self.document_version(result.chunk.document_id)
        version_hash = version.removeprefix("sha256:")
        return f"rag:{result.chunk.document_id}:{version_hash}:{result.chunk.chunk_id}"

    def to_search_result(self, result: SearchResult) -> dict[str, Any]:
        chunk = result.chunk
        return {
            "content": chunk.content,
            "score": round(result.score, 6),
            "documentId": chunk.document_id,
            "documentVersion": self.document_version(chunk.document_id),
            "title": chunk.title,
            "contentType": chunk.content_type,
            "chunkId": chunk.chunk_id,
            "sectionPath": chunk.section_path,
            "pageStart": chunk.page_start,
            "pageEnd": chunk.page_end,
            "resourceRef": self.resource_ref(result),
            "warnings": chunk.warnings,
        }

    def to_citation(self, result: SearchResult, run_id: str, ordinal: int) -> dict[str, Any]:
        chunk = result.chunk
        citation_hash = hashlib.sha256(f"{run_id}:{chunk.chunk_id}".encode()).hexdigest()[:24]
        return {
            "citationId": f"cit-{citation_hash}",
            "ordinal": ordinal,
            "documentId": chunk.document_id,
            "documentVersion": self.document_version(chunk.document_id),
            "title": chunk.title,
            "contentType": chunk.content_type,
            "sectionPath": chunk.section_path,
            "pageStart": chunk.page_start,
            "pageEnd": chunk.page_end,
            "chunkId": chunk.chunk_id,
            "excerpt": "",
            "excerptAllowed": False,
            "score": round(result.score, 6),
            "source": {"resourceRef": self.resource_ref(result)},
            "warnings": chunk.warnings,
        }

    def format_context(self, results: list[SearchResult]) -> str:
        return self.retriever.format_context(results)
