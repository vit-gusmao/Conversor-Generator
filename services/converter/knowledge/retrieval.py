"""Lexical retrieval baseline with citations.

The baseline is intentionally dependency-free: BM25-style scoring over chunks
already filtered by owner and collection.  An embedding model can be added
later behind the same ``retrieve`` contract; the ACL rule does not change.

Citations are validated against the chunks returned by *this* request.  A
model can only cite an id that exists in the retrieval set, which prevents an
invented or cross-owner source from being rendered.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from knowledge.repository import KnowledgeRepository

_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
    "nos", "nas", "um", "uma", "para", "por", "com", "que", "se", "ao", "aos",
    "the", "of", "and", "to", "in", "is", "it", "for", "on", "with", "what",
    "como", "qual", "quais", "quando", "onde", "porque",
}


@dataclass(slots=True)
class Citation:
    chunk_id: str
    document_id: str
    document_name: str | None
    run_id: str | None
    conversion_id: str | None
    revision_id: str | None
    page_number: int | None
    excerpt: str
    offset_start: int
    offset_end: int
    score: float
    token_count: int

    def to_dict(self, index: int | None = None) -> dict[str, Any]:
        payload = {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "run_id": self.run_id,
            "conversion_id": self.conversion_id,
            "revision_id": self.revision_id,
            "page_number": self.page_number,
            "excerpt": self.excerpt,
            "offset_start": self.offset_start,
            "offset_end": self.offset_end,
            "score": round(self.score, 4),
            "token_count": self.token_count,
        }
        if index is not None:
            payload["index"] = index
        return payload


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def tokenize(text: str) -> list[str]:
    return [word for word in _WORD.findall(normalize(text)) if word not in STOPWORDS and len(word) > 1]


def retrieve(
    *,
    owner_id: str,
    query: str,
    collection_ids: list[str] | None = None,
    top_k: int = 8,
    repository: KnowledgeRepository | None = None,
) -> list[Citation]:
    repo = repository or KnowledgeRepository()
    # ACL first: candidates only ever contain this owner's chunks.
    candidates = repo.candidates(owner_id, collection_ids)
    if not candidates:
        return []
    terms = tokenize(query)
    if not terms:
        return []

    tokenised: list[list[str]] = [tokenize(chunk["text"]) for chunk in candidates]
    total = len(candidates)
    average_length = sum(len(tokens) for tokens in tokenised) / max(1, total)
    document_frequency = Counter()
    for tokens in tokenised:
        for term in set(tokens):
            document_frequency[term] += 1

    normalized_query = normalize(query)
    scored: list[tuple[float, int]] = []
    for index, tokens in enumerate(tokenised):
        if not tokens:
            continue
        counts = Counter(tokens)
        length_norm = 0.25 + 0.75 * (len(tokens) / max(1.0, average_length))
        score = 0.0
        for term in set(terms):
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1.0 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += idf * (frequency * 2.2) / (frequency + 1.2 * length_norm)
        if normalized_query and normalized_query in normalize(candidates[index]["text"]):
            score += 2.5
        if score > 0:
            scored.append((score, index))

    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[Citation] = []
    for score, index in scored[: max(1, top_k)]:
        chunk = candidates[index]
        metadata = chunk.get("metadata") or {}
        results.append(
            Citation(
                chunk_id=chunk["chunk_id"],
                document_id=chunk["document_id"],
                document_name=metadata.get("document_name")
                or metadata.get("source_path")
                or chunk["document_id"],
                run_id=chunk.get("run_id"),
                conversion_id=metadata.get("conversion_id"),
                revision_id=chunk.get("revision_id"),
                page_number=chunk.get("page_number"),
                excerpt=_excerpt(chunk["text"], terms),
                offset_start=int(chunk.get("offset_start") or 0),
                offset_end=int(chunk.get("offset_end") or 0),
                score=score,
                token_count=int(chunk.get("token_count") or 0),
            )
        )
    return results


def build_context(citations: list[Citation], *, max_chars: int = 12_000) -> str:
    """Render numbered sources for the prompt (data, never instructions)."""
    parts: list[str] = []
    used = 0
    for index, citation in enumerate(citations, start=1):
        header = (
            f"[{index}] documento={citation.document_name or citation.document_id}"
            f" pagina={citation.page_number if citation.page_number is not None else 'n/d'}"
        )
        body = citation.excerpt
        block = f"{header}\n{body}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _excerpt(text: str, terms: list[str], limit: int = 600) -> str:
    normalized = normalize(text)
    position = -1
    for term in terms:
        found = normalized.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[:limit] + ("…" if len(text) > limit else "")
    start = max(0, position - limit // 3)
    end = min(len(text), start + limit)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


__all__ = [
    "Citation",
    "STOPWORDS",
    "build_context",
    "normalize",
    "retrieve",
    "tokenize",
]
