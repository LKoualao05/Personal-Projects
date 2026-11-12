"""Text embeddings: Gemini embeddings on Vertex AI, plus an offline hashing embedder."""

from __future__ import annotations

import hashlib
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

DOCUMENT = "RETRIEVAL_DOCUMENT"
QUERY = "RETRIEVAL_QUERY"


class Embedder(Protocol):
    name: str
    dims: int
    tokens_used: int

    def embed(self, texts: list[str], task: str = DOCUMENT) -> list[list[float]]: ...


class GeminiEmbedder:
    """Vertex AI ``gemini-embedding-001`` (or any embedding model) via google-genai.

    One text per request (the Vertex Gemini embedding endpoint accepts a
    single input), parallelised with a small thread pool.
    """

    def __init__(self, model: str, dims: int, project: str = "", location: str = "us-central1",
                 client: Any = None, max_workers: int = 4):
        self.name = model
        self.dims = dims
        self.max_workers = max_workers
        self.tokens_used = 0
        self._lock = threading.Lock()
        if client is None:
            from ..llm import make_genai_client

            client = make_genai_client(project, location)
        self.client = client

    def _one(self, text: str, task: str) -> list[float]:
        from google.genai import types

        resp = self.client.models.embed_content(
            model=self.name,
            contents=text,
            config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims),
        )
        emb = resp.embeddings[0]
        stats = getattr(emb, "statistics", None)
        tokens = int(getattr(stats, "token_count", 0) or 0) or max(1, len(text) // 4)
        with self._lock:
            self.tokens_used += tokens
        return list(emb.values)

    def embed(self, texts: list[str], task: str = DOCUMENT) -> list[list[float]]:
        if not texts:
            return []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(texts))) as pool:
            return list(pool.map(lambda t: self._one(t, task), texts))


class HashingEmbedder:
    """Deterministic bag-of-words embedder for tests and offline demos (no API calls)."""

    name = "hashing"

    def __init__(self, dims: int = 256):
        self.dims = dims
        self.tokens_used = 0

    def embed(self, texts: list[str], task: str = DOCUMENT) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for tok in re.findall(r"[a-z0-9+#]+", text.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
            self.tokens_used += len(text) // 4
        return out


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
