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


def _chunk_plan(*segments: str, max_tokens: int, overlap: int):
    plan = _plan((1, list(segments)))
    return chunk_text_plan(
        plan,
        owner_id="o",
        collection_id="c",
        document_id="d",
        run_id="r",
        min_tokens=max_tokens,
        max_tokens=max_tokens,
        overlap=overlap,
    )


def test_no_chunk_is_only_overlap_tail_after_long_segment():
    # AU2-24: an overlap tail carried over a long segment must not become a
    # standalone chunk at the end of the page.
    long_text = " ".join(f"w{i}" for i in range(1, 15))
    drafts = _chunk_plan("a1 a2", "b1 b2", long_text, max_tokens=5, overlap=3)
    texts = [draft.text for draft in drafts]
    assert "b1 b2" not in texts[1:]
    for text in texts[1:]:
        assert text in long_text, text


def test_chunks_never_exceed_max_tokens_after_overlap_tail():
    # AU2-24: the tail carried into the next chunk must still respect max_tokens.
    drafts = _chunk_plan("a1 a2", "b1 b2", "c1 c2 c3 c4", max_tokens=5, overlap=2)
    assert drafts
    assert all(draft.token_count <= 5 for draft in drafts), [d.text for d in drafts]


def test_study_paragraphs_split_on_crlf_blank_lines():
    # AU2-25: CRLF paragraph separators must split paragraphs like LF ones do.
    from knowledge.chunker import chunk_study_text

    drafts = chunk_study_text(
        "alpha beta\r\n\r\ngamma delta",
        owner_id="o",
        collection_id="c",
        document_id="d",
        source_path="x.md",
        max_tokens=2,
        overlap=0,
    )
    assert [draft.text for draft in drafts] == ["alpha beta", "gamma delta"]


def test_study_chunks_never_exceed_max_tokens_with_overlap():
    from knowledge.chunker import chunk_study_text

    text = "l1a l1b l1c l1d l1e l1f\nl2a l2b l2c l2d\nl3a l3b l3c l3d l3e l3f l3g\nl4a l4b"
    drafts = chunk_study_text(
        text,
        owner_id="o",
        collection_id="c",
        document_id="d",
        source_path="x.md",
        max_tokens=10,
        overlap=8,
    )
    assert drafts
    assert all(draft.token_count <= 10 for draft in drafts), [d.text for d in drafts]


def test_unnamed_collection_submits_reuse_one_collection_per_owner():
    # AU2-17: the lookup must use the same resolved name as the create path.
    import uuid

    repo = KnowledgeRepository()
    owner = f"owner-{uuid.uuid4().hex}"
    first = repo.ensure_collection(owner_id=owner, collection_id=None, name=None, scope="documents")
    second = repo.ensure_collection(owner_id=owner, collection_id=None, name="  ", scope="documents")
    third = repo.ensure_collection(owner_id=owner, collection_id=None, name="", scope="documents")
    assert first["name"] == "Documentos"
    assert second["collection_id"] == first["collection_id"]
    assert third["collection_id"] == first["collection_id"]
    assert len(repo.list_collections(owner)) == 1
