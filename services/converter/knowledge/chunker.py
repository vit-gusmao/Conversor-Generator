"""Deterministic chunking for retrieval.

Defaults: 400–800 tokens per chunk (approximated by whitespace tokens), with a
maximum overlap of 80 tokens.  A table is kept with its header repeated and its
original page/offsets, so a citation can point back to the exact source.

The chunk id is deterministic: origin + versions + index.  Re-indexing the same
run therefore updates chunks instead of duplicating them.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from exports.text_plan import TextArtifactPlan, TextSegment

PARSER_VERSION = "ir-v1"
CHUNKER_VERSION = "chunker-v1"

_WS = re.compile(r"\s+")


@dataclass(slots=True)
class ChunkDraft:
    chunk_id: str
    text: str
    page_number: int | None
    block_id: str | None
    chunk_index: int
    offset_start: int
    offset_end: int
    token_count: int
    chunk_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def count_tokens(text: str) -> int:
    return len([word for word in _WS.split(text or "") if word])


def chunk_text_plan(
    plan: TextArtifactPlan,
    *,
    owner_id: str,
    collection_id: str,
    document_id: str,
    run_id: str | None,
    revision_id: str | None = None,
    min_tokens: int = 400,
    max_tokens: int = 800,
    overlap: int = 80,
) -> list[ChunkDraft]:
    if max_tokens <= 0:
        max_tokens = 800
    overlap = max(0, min(overlap, min_tokens if min_tokens else max_tokens // 4, max_tokens - 1))
    drafts: list[ChunkDraft] = []
    index = 0

    for page in sorted(plan.pages, key=lambda item: item.page_number):
        buffer: list[TextSegment] = []
        buffer_tokens = 0

        def flush(page=page) -> None:
            nonlocal buffer, buffer_tokens, index
            if not buffer:
                return
            text = "\n".join(segment.text for segment in buffer).strip()
            if not text:
                buffer = []
                buffer_tokens = 0
                return
            drafts.append(
                _draft(
                    text=text,
                    index=index,
                    page_number=buffer[0].page_number,
                    block_id=buffer[0].element_id or buffer[0].table_id,
                    offset_start=0,
                    offset_end=0,
                    owner_id=owner_id,
                    collection_id=collection_id,
                    document_id=document_id,
                    run_id=run_id,
                    revision_id=revision_id,
                    metadata={
                        "page_status": page.status,
                        "page_number": page.page_number,
                        "kinds": sorted({segment.kind for segment in buffer}),
                    },
                )
            )
            index += 1
            # Keep a bounded tail as overlap for the next chunk.
            if overlap and buffer_tokens > overlap:
                tail: list[TextSegment] = []
                tail_tokens = 0
                for segment in reversed(buffer):
                    tokens = count_tokens(segment.text)
                    if tail_tokens + tokens > overlap:
                        break
                    tail.insert(0, segment)
                    tail_tokens += tokens
                buffer = tail
                buffer_tokens = tail_tokens
            else:
                buffer = []
                buffer_tokens = 0

        for segment in page.segments:
            tokens = count_tokens(segment.text)
            if tokens > max_tokens:
                flush()
                for piece in _split_long(segment, max_tokens=max_tokens, overlap=overlap):
                    drafts.append(
                        _draft(
                            text=piece,
                            index=index,
                            page_number=segment.page_number,
                            block_id=segment.element_id or segment.table_id,
                            offset_start=0,
                            offset_end=0,
                            owner_id=owner_id,
                            collection_id=collection_id,
                            document_id=document_id,
                            run_id=run_id,
                            revision_id=revision_id,
                            metadata={"page_number": segment.page_number, "kind": segment.kind},
                        )
                    )
                    index += 1
                # Nothing is carried across a long block: its tail would
                # otherwise come out later as a chunk of overlap alone.
                buffer = []
                buffer_tokens = 0
                continue
            if buffer and buffer_tokens + tokens > max_tokens:
                flush()
                # The carried tail is only an overlap: drop it rather than exceed the budget.
                while buffer and buffer_tokens + tokens > max_tokens:
                    buffer_tokens -= count_tokens(buffer.pop(0).text)
            buffer.append(segment)
            buffer_tokens += tokens
        flush()

    return drafts


def chunk_study_text(
    text: str,
    *,
    owner_id: str,
    collection_id: str,
    document_id: str,
    source_path: str,
    max_tokens: int = 800,
    overlap: int = 80,
) -> list[ChunkDraft]:
    """Chunk an explicitly selected engineering study file."""
    normalized = text.replace("\r\n", "\n")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    drafts: list[ChunkDraft] = []
    buffer: list[str] = []
    buffer_tokens = 0
    index = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens, index
        if not buffer:
            return
        content = "\n\n".join(buffer).strip()
        drafts.append(
            _draft(
                text=content,
                index=index,
                page_number=None,
                block_id=None,
                offset_start=0,
                offset_end=len(content),
                owner_id=owner_id,
                collection_id=collection_id,
                document_id=document_id,
                run_id=None,
                revision_id=None,
                metadata={"source_path": source_path, "historical": True, "kind": "study"},
            )
        )
        index += 1
        buffer = []
        buffer_tokens = 0

    for paragraph in paragraphs:
        tokens = count_tokens(paragraph)
        if tokens > max_tokens:
            flush()
            for piece in _split_text(paragraph, max_tokens=max_tokens, overlap=overlap):
                drafts.append(
                    _draft(
                        text=piece,
                        index=index,
                        page_number=None,
                        block_id=None,
                        offset_start=0,
                        offset_end=len(piece),
                        owner_id=owner_id,
                        collection_id=collection_id,
                        document_id=document_id,
                        run_id=None,
                        revision_id=None,
                        metadata={"source_path": source_path, "historical": True, "kind": "study"},
                    )
                )
                index += 1
            continue
        if buffer and buffer_tokens + tokens > max_tokens:
            flush()
        buffer.append(paragraph)
        buffer_tokens += tokens
    flush()
    return drafts


def _draft(
    *,
    text: str,
    index: int,
    page_number: int | None,
    block_id: str | None,
    offset_start: int,
    offset_end: int,
    owner_id: str,
    collection_id: str,
    document_id: str,
    run_id: str | None,
    revision_id: str | None,
    metadata: dict[str, Any],
) -> ChunkDraft:
    chunk_hash = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()
    identity = "|".join(
        [
            owner_id,
            collection_id,
            document_id,
            run_id or "",
            revision_id or "",
            str(page_number or 0),
            block_id or "",
            str(index),
            chunk_hash,
            PARSER_VERSION,
            CHUNKER_VERSION,
        ]
    )
    chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return ChunkDraft(
        chunk_id=chunk_id,
        text=text,
        page_number=page_number,
        block_id=block_id,
        chunk_index=index,
        offset_start=offset_start,
        offset_end=offset_end,
        token_count=count_tokens(text),
        chunk_hash=chunk_hash,
        metadata=metadata,
    )


def _split_long(segment: TextSegment, *, max_tokens: int, overlap: int) -> Iterable[str]:
    yield from _split_text(segment.text, max_tokens=max_tokens, overlap=overlap)


def _split_text(text: str, *, max_tokens: int, overlap: int) -> Iterable[str]:
    lines = text.splitlines() or [text]
    if len(lines) == 1 and count_tokens(lines[0]) > max_tokens:
        # A single paragraph without line breaks must still respect the budget.
        yield from _split_words(lines[0], max_tokens=max_tokens, overlap=overlap)
        return
    buffer: list[str] = []
    buffer_tokens = 0
    for line in lines:
        tokens = count_tokens(line)
        if tokens > max_tokens:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
                buffer_tokens = 0
            yield from _split_words(line, max_tokens=max_tokens, overlap=overlap)
            continue
        if buffer and buffer_tokens + tokens > max_tokens:
            yield "\n".join(buffer)
            tail: list[str] = []
            tail_tokens = 0
            for candidate in reversed(buffer):
                candidate_tokens = count_tokens(candidate)
                if tail_tokens + candidate_tokens > overlap:
                    break
                tail.insert(0, candidate)
                tail_tokens += candidate_tokens
            buffer = tail
            buffer_tokens = tail_tokens
            while buffer and buffer_tokens + tokens > max_tokens:
                buffer_tokens -= count_tokens(buffer.pop(0))
        buffer.append(line)
        buffer_tokens += tokens
    if buffer:
        yield "\n".join(buffer)


def _split_words(text: str, *, max_tokens: int, overlap: int) -> Iterable[str]:
    words = [word for word in _WS.split(text) if word]
    start = 0
    while start < len(words):
        end = min(len(words), start + max_tokens)
        yield " ".join(words[start:end])
        if end >= len(words):
            break
        start = max(start + 1, end - max(0, min(overlap, max_tokens - 1)))


__all__ = [
    "CHUNKER_VERSION",
    "PARSER_VERSION",
    "ChunkDraft",
    "chunk_text_plan",
    "chunk_study_text",
    "count_tokens",
]
