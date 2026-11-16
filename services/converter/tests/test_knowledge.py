"""Knowledge base: deterministic chunks, ACL isolation and citations."""

from __future__ import annotations

import pytest

from exports.text_plan import TextArtifactPlan, TextPage, TextSegment
from knowledge.chunker import chunk_text_plan
from knowledge.repository import KnowledgeRepository
from knowledge.retrieval import retrieve
from models.db import init_db


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _plan(*pages: tuple[int, list[str]]) -> TextArtifactPlan:
    return TextArtifactPlan(
        pages=[
            TextPage(
                page_number=number,
                status="ok",
                segments=[
                    TextSegment(text=text, page_number=number, kind="text", order=index)
                    for index, text in enumerate(texts)
                ],
            )
            for number, texts in pages
        ],
        source_name="documento.pdf",
        source_sha256="abc",
    )


def test_chunks_are_deterministic_and_keep_page():
    plan = _plan((1, ["O total é 42 reais. " * 200]), (2, ["Segunda página."]))
    first = chunk_text_plan(
        plan, owner_id="u1", collection_id="c1", document_id="d1", run_id="r1", max_tokens=200, overlap=20
    )
    second = chunk_text_plan(
        plan, owner_id="u1", collection_id="c1", document_id="d1", run_id="r1", max_tokens=200, overlap=20
    )
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert {chunk.page_number for chunk in first} == {1, 2}
    assert all(chunk.token_count <= 200 for chunk in first)


def test_retrieval_is_scoped_to_owner_and_collection():
    repo = KnowledgeRepository()
    collection_a = repo.create_collection(owner_id="owner_a", name="A")
    collection_b = repo.create_collection(owner_id="owner_b", name="B")
    plan_a = _plan((1, ["O contrato prevê multa de 10%."]))
    plan_b = _plan((1, ["Segredo do usuário B: multa de 99%."]))
    drafts_a = chunk_text_plan(
        plan_a, owner_id="owner_a", collection_id=collection_a["collection_id"], document_id="da", run_id="ra"
    )
    drafts_b = chunk_text_plan(
        plan_b, owner_id="owner_b", collection_id=collection_b["collection_id"], document_id="db", run_id="rb"
    )
    repo.replace_document_chunks(
        owner_id="owner_a", collection_id=collection_a["collection_id"], document_id="da", drafts=drafts_a
    )
    repo.replace_document_chunks(
        owner_id="owner_b", collection_id=collection_b["collection_id"], document_id="db", drafts=drafts_b
    )

    results_a = retrieve(owner_id="owner_a", query="multa", repository=repo)
    assert results_a
    assert all("Segredo" not in item.excerpt for item in results_a)
    assert all(item.chunk_id in {draft.chunk_id for draft in drafts_a} for item in results_a)

    results_b = retrieve(owner_id="owner_b", query="multa", repository=repo)
    assert results_b
    assert all(item.chunk_id in {draft.chunk_id for draft in drafts_b} for item in results_b)


def test_retrieval_respects_selected_collection():
    repo = KnowledgeRepository()
    first = repo.create_collection(owner_id="u2", name="Primeira")
    second = repo.create_collection(owner_id="u2", name="Segunda")
    for collection, text in ((first, "alfa bravo"), (second, "alfa charlie")):
        drafts = chunk_text_plan(
            _plan((1, [text])),
            owner_id="u2",
            collection_id=collection["collection_id"],
            document_id=collection["collection_id"],
            run_id="r",
        )
        repo.replace_document_chunks(
            owner_id="u2",
            collection_id=collection["collection_id"],
            document_id=collection["collection_id"],
            drafts=drafts,
        )
    results = retrieve(
        owner_id="u2",
        query="alfa",
        collection_ids=[second["collection_id"]],
        repository=repo,
    )
    assert results
    assert all("charlie" in item.excerpt for item in results)
