from __future__ import annotations

from typing import Any, TypedDict


class RagState(TypedDict, total=False):
    query: str
    top_k: int
    filters: dict[str, list[str]]
    retrieval_results: list[dict[str, Any]]
    context: str
    citations: list[str]
    warnings: list[str]
    has_evidence: bool

