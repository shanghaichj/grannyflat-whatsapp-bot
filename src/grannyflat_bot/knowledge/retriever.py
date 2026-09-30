from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from grannyflat_bot.knowledge.ingest import Chunk, ingest_knowledge
from grannyflat_bot.llm.client import LLMClient


@dataclass(frozen=True)
class KnowledgeHit:
    chunk: Chunk
    score: float
    keyword_score: float


@dataclass
class KnowledgeRetriever:
    llm: LLMClient
    chunks: list[Chunk] = field(default_factory=list)
    embeddings: np.ndarray | None = None

    @classmethod
    def build(cls, llm: LLMClient | None = None) -> KnowledgeRetriever:
        client = llm or LLMClient()
        chunks = ingest_knowledge()
        retriever = cls(llm=client, chunks=chunks)
        retriever.rebuild_index()
        return retriever

    def rebuild_index(self) -> None:
        if not self.chunks:
            self.embeddings = None
            return
        vectors = self.llm.embed([c.text for c in self.chunks])
        self.embeddings = np.array(vectors, dtype=np.float32)

    def retrieve_scored(
        self, query: str, *, top_k: int = 4, min_score: float = 0.18
    ) -> list[KnowledgeHit]:
        if not self.chunks or self.embeddings is None:
            return []
        if not (query or "").strip():
            return []
        q = np.array(self.llm.embed([query])[0], dtype=np.float32)
        # cosine similarity
        denom = (np.linalg.norm(self.embeddings, axis=1) * (np.linalg.norm(q) or 1.0))
        denom = np.where(denom == 0, 1.0, denom)
        scores = (self.embeddings @ q) / denom
        # Also boost keyword overlap for mock embeddings
        keyword_scores = _keyword_boost(query, self.chunks)
        scores = scores + keyword_scores * 0.25
        top_idx = np.argsort(-scores)[:top_k]
        hits: list[KnowledgeHit] = []
        for raw_index in top_idx:
            index = int(raw_index)
            score = float(scores[index])
            keyword_score = float(keyword_scores[index])
            if not math.isfinite(score):
                continue
            if score < min_score and keyword_score <= 0:
                continue
            hits.append(KnowledgeHit(self.chunks[index], score, keyword_score))
        return hits

    def retrieve(self, query: str, *, top_k: int = 4) -> list[Chunk]:
        return [hit.chunk for hit in self.retrieve_scored(query, top_k=top_k)]

    def context_bundle(self, query: str, *, top_k: int = 4) -> dict[str, object]:
        hits = self.retrieve_scored(query, top_k=top_k)
        blocks = [f"[{hit.chunk.source}#{hit.chunk.id}]\n{hit.chunk.text}" for hit in hits]
        return {
            "context": "\n\n---\n\n".join(blocks),
            "sources": [
                {
                    "id": hit.chunk.id,
                    "source": hit.chunk.source,
                    "score": round(hit.score, 4),
                }
                for hit in hits
            ],
            "confidence": _confidence_label(hits),
        }

    def format_context(self, query: str, *, top_k: int = 4) -> str:
        return str(self.context_bundle(query, top_k=top_k)["context"])


def _keyword_boost(query: str, chunks: list[Chunk]) -> np.ndarray:
    stopwords = {
        "the", "and", "for", "are", "you", "your", "with", "this", "that",
        "what", "how", "can", "our", "from", "about", "have", "need",
    }
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", query.lower())
        if len(token) >= 2 and token not in stopwords
    }
    boosts = []
    for chunk in chunks:
        words = set(
            re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", chunk.text.lower())
        )
        overlap = len(tokens & words)
        boosts.append(float(overlap))
    arr = np.array(boosts, dtype=np.float32)
    if arr.max() > 0:
        arr = arr / arr.max()
    return arr


def _confidence_label(hits: list[KnowledgeHit]) -> str:
    if not hits:
        return "none"
    top = hits[0]
    if top.keyword_score > 0 and top.score >= 0.45:
        return "high"
    if top.keyword_score > 0 or top.score >= 0.28:
        return "medium"
    return "low"
