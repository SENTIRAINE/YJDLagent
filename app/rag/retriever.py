from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

import numpy as np

from app.config import Settings
from app.rag.embeddings import create_embedding_provider, lexical_tokens
from app.rag.models import Chunk, SearchResult
from app.rag.store import SQLiteVectorStore


NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
FORMULA_INTENTS = ("如何计算", "怎么计算", "计算公式", "公式是什么", "计算方法")
UNCERTAINTY_INTENTS = ("是否矛盾", "原文疑点", "口径", "准确吗", "为什么不一致")


def _bm25_scores(query_tokens: list[str], documents: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    if not documents:
        return []
    query_terms = set(query_tokens)
    frequencies = [Counter(document) for document in documents]
    document_frequency = {
        term: sum(1 for frequency in frequencies if frequency.get(term, 0)) for term in query_terms
    }
    average_length = sum(len(document) for document in documents) / len(documents) or 1.0
    scores: list[float] = []
    for document, frequency in zip(documents, frequencies, strict=True):
        score = 0.0
        for term in query_terms:
            tf = frequency.get(term, 0)
            if not tf:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * len(document) / average_length)
            score += idf * tf * (k1 + 1) / denominator
        scores.append(score)
    return scores


class HybridRetriever:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.store = SQLiteVectorStore(self.settings.database_path)
        metadata = self.store.metadata()
        self.provider = create_embedding_provider(self.settings)
        if metadata["embedding_provider"] != self.provider.name:
            raise ValueError(
                "Configured embedding provider does not match the built index: "
                f"{self.provider.name!r} != {metadata['embedding_provider']!r}"
            )
        if int(metadata["embedding_dimension"]) != self.provider.dimension:
            raise ValueError("Configured embedding dimension does not match the built index")
        self.entries = self.store.load_chunks()
        self.document_tokens = [lexical_tokens(chunk.content) for chunk, _ in self.entries]

    def search(
        self,
        query: str,
        top_k: int | None = None,
        document_ids: Iterable[str] | None = None,
        content_types: Iterable[str] | None = None,
    ) -> list[SearchResult]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        top_k = top_k or self.settings.default_top_k
        if top_k <= 0 or top_k > 50:
            raise ValueError("top_k must be between 1 and 50")

        allowed_documents = set(document_ids or [])
        allowed_types = set(content_types or [])
        selected = [
            index
            for index, (chunk, _) in enumerate(self.entries)
            if (not allowed_documents or chunk.document_id in allowed_documents)
            and (not allowed_types or chunk.content_type in allowed_types)
        ]
        if not selected:
            return []

        query_vector = self.provider.embed([query])[0]
        selected_tokens = [self.document_tokens[index] for index in selected]
        raw_lexical = _bm25_scores(lexical_tokens(query), selected_tokens)
        lexical_max = max(raw_lexical, default=0.0)
        query_numbers = set(NUMBER_RE.findall(query))

        results: list[SearchResult] = []
        for position, index in enumerate(selected):
            chunk, vector = self.entries[index]
            dense = max(0.0, float(np.dot(query_vector, vector)))
            lexical = raw_lexical[position] / lexical_max if lexical_max else 0.0
            chunk_numbers = set(NUMBER_RE.findall(chunk.content))
            number = len(query_numbers & chunk_numbers) / len(query_numbers) if query_numbers else 0.0
            intent = 0.0
            if ("公式" in query or any(marker in query for marker in FORMULA_INTENTS)) and chunk.content_type == "formula":
                intent += 0.18
            if any(marker in query for marker in UNCERTAINTY_INTENTS) and chunk.warnings:
                intent += 0.12
            score = min(1.0, (
                self.settings.dense_weight * dense
                + self.settings.lexical_weight * lexical
                + self.settings.number_weight * number
                + intent
            ))
            results.append(
                SearchResult(
                    chunk=chunk,
                    score=score,
                    dense_score=dense,
                    lexical_score=lexical,
                    number_score=number,
                    intent_score=intent,
                )
            )
        results.sort(key=lambda result: (-result.score, result.chunk.ordinal))
        return results[:top_k]

    @staticmethod
    def format_context(results: list[SearchResult]) -> str:
        blocks = []
        for index, result in enumerate(results, start=1):
            warning = "\n来源提示：该片段含原文疑点，回答时必须显式说明。" if result.chunk.warnings else ""
            blocks.append(
                f"[证据 {index}] {result.citation}\n"
                f"内容类型：{result.chunk.content_type}{warning}\n"
                f"{result.chunk.content}"
            )
        return "\n\n".join(blocks)
