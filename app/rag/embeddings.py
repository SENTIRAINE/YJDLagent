from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from app.config import Settings


TOKEN_RE = re.compile(r"[\u3400-\u9fff]+|[a-zA-Z]+(?:[._-][a-zA-Z0-9]+)*|\d+(?:\.\d+)?%?")


def lexical_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(normalized):
        value = match.group(0)
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            tokens.extend(value)
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
            if len(value) <= 8:
                tokens.append(value)
        else:
            tokens.append(value)
    return tokens


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return one normalized float32 vector per input string."""


class HashingEmbedding:
    """Offline deterministic baseline; use BGE-M3 for production semantics."""

    name = "hash"

    def __init__(self, dimension: int = 768):
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: dict[str, int] = {}
            for token in lexical_tokens(text):
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "little") % self.dimension
                sign = 1.0 if digest[8] & 1 else -1.0
                matrix[row, index] += sign * (1.0 + math.log(count))
            norm = float(np.linalg.norm(matrix[row]))
            if norm:
                matrix[row] /= norm
        return matrix


class OpenAICompatibleEmbedding:
    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key: str, dimension: int):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        payload = json.dumps({"model": self.model, "input": list(texts)}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Embedding service request failed: {exc}") from exc
        rows = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
        if matrix.shape != (len(texts), self.dimension):
            raise ValueError(
                f"Embedding response shape {matrix.shape} does not match expected {(len(texts), self.dimension)}"
            )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return matrix / norms


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "hash":
        return HashingEmbedding(settings.embedding_dimension)
    if settings.embedding_provider in {"openai", "openai-compatible", "bge-m3"}:
        return OpenAICompatibleEmbedding(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            dimension=settings.embedding_dimension,
        )
    raise ValueError(f"Unsupported RAG_EMBEDDING_PROVIDER: {settings.embedding_provider}")

