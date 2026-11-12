"""Vector stores for job-description chunks.

``FirestoreVectorStore`` uses Firestore's native KNN vector search
(``find_nearest`` with cosine distance). It needs a vector index on the
``embedding`` field, created once (see README):

    gcloud firestore indexes composite create --collection-group=job_description_chunks \\
        --query-scope=COLLECTION --field-config=field-path=embedding,vector-config='{"dimension":"768","flat":"{}"}'

``LocalVectorStore`` is a JSON file with brute-force cosine search, for tests,
offline use and the eval harness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from .embeddings import cosine


@dataclass
class JDChunk:
    job_key: str
    company: str
    role_title: str
    chunk_index: int
    text: str
    embedding: list[float] = field(default_factory=list, repr=False)
    job_id: str = ""
    source: str = ""  # posting | email | manual
    source_url: str = ""
    message_id: str = ""

    @property
    def doc_id(self) -> str:
        return f"{self.job_key}_{self.chunk_index:03d}"

    def metadata(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("embedding")
        return d


@dataclass
class SearchHit:
    chunk: JDChunk
    score: float  # cosine similarity, higher is better


def _company_match(chunk_company: str, wanted: str) -> bool:
    return not wanted or wanted.lower() in chunk_company.lower()


class VectorStore(Protocol):
    def upsert(self, chunks: list[JDChunk]) -> None: ...
    def delete_job(self, job_key: str) -> None: ...
    def has_job(self, job_key: str) -> bool: ...
    def search(self, vector: list[float], k: int = 5, company: str = "") -> list[SearchHit]: ...
    def count(self) -> int: ...


class LocalVectorStore:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self.chunks: dict[str, JDChunk] = {}
        if self.path and self.path.exists():
            for d in json.loads(self.path.read_text()):
                c = JDChunk(**d)
                self.chunks[c.doc_id] = c

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps([asdict(c) for c in self.chunks.values()]))

    def upsert(self, chunks: list[JDChunk]) -> None:
        for key in {c.job_key for c in chunks}:
            self._drop(key)
        for c in chunks:
            self.chunks[c.doc_id] = c
        self._save()

    def _drop(self, job_key: str) -> None:
        for doc_id in [d for d, c in self.chunks.items() if c.job_key == job_key]:
            del self.chunks[doc_id]

    def delete_job(self, job_key: str) -> None:
        self._drop(job_key)
        self._save()

    def has_job(self, job_key: str) -> bool:
        return any(c.job_key == job_key for c in self.chunks.values())

    def search(self, vector: list[float], k: int = 5, company: str = "") -> list[SearchHit]:
        hits = [SearchHit(c, cosine(vector, c.embedding)) for c in self.chunks.values()
                if _company_match(c.company, company)]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]

    def count(self) -> int:
        return len(self.chunks)


class FirestoreVectorStore:
    def __init__(self, project: str, database: str = "(default)", collection: str = "job_description_chunks",
                 client: Any = None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project or None, database=database)
        self.client = client
        self.col = client.collection(collection)

    def upsert(self, chunks: list[JDChunk]) -> None:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP
        from google.cloud.firestore_v1.vector import Vector

        for key in {c.job_key for c in chunks}:
            self.delete_job(key)
        batch = self.client.batch()
        for i, c in enumerate(chunks, start=1):
            data = c.metadata() | {"embedding": Vector(c.embedding), "updated_at": SERVER_TIMESTAMP}
            batch.set(self.col.document(c.doc_id), data)
            if i % 400 == 0:  # Firestore batch limit is 500 writes
                batch.commit()
                batch = self.client.batch()
        batch.commit()

    def _job_docs(self, job_key: str, limit: Optional[int] = None):
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self.col.where(filter=FieldFilter("job_key", "==", job_key))
        return (q.limit(limit) if limit else q).get()

    def delete_job(self, job_key: str) -> None:
        for snap in self._job_docs(job_key):
            snap.reference.delete()

    def has_job(self, job_key: str) -> bool:
        return len(list(self._job_docs(job_key, limit=1))) > 0

    def search(self, vector: list[float], k: int = 5, company: str = "") -> list[SearchHit]:
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        # Company filtering happens after retrieval, so over-fetch when filtering.
        limit = min(1000, k * 5 if company else k)
        snaps = self.col.find_nearest(
            vector_field="embedding", query_vector=Vector(vector), limit=limit,
            distance_measure=DistanceMeasure.COSINE, distance_result_field="vector_distance",
        ).get()
        hits = []
        for s in snaps:
            d = s.to_dict()
            distance = float(d.pop("vector_distance", 1.0))
            d.pop("embedding", None)
            d.pop("updated_at", None)
            chunk = JDChunk(**{k_: d.get(k_, "") for k_ in JDChunk.__dataclass_fields__ if k_ != "embedding"})
            chunk.chunk_index = int(chunk.chunk_index or 0)
            if _company_match(chunk.company, company):
                hits.append(SearchHit(chunk, 1.0 - distance))
        return hits[:k]

    def count(self) -> int:
        result = self.col.count().get()
        return int(result[0][0].value)
