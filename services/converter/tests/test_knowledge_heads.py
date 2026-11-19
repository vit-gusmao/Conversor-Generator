"""Source-backed knowledge heads: only the active generation is searchable.

Two versions of the same imported file share a logical identity
``(owner, collection, source_id, relative_path)``. The head points at the
active document; a failed or stale update must never move it, and a vanished
origin is flagged on citations instead of hiding the content.
"""

from __future__ import annotations

import uuid

import pytest

from knowledge.indexer import KnowledgeIndexer
from knowledge.job_runner import KnowledgeJobRunner
from knowledge.repository import KnowledgeRepository
from knowledge.retrieval import build_context, retrieve
from models.db import PublicConversionRow, PublicDocumentRow, get_session_factory, init_db
from repositories.conversion_repository import ConversionRepository
from sources.models import SourceImportItemRow, SourceImportRow, SourceKnowledgeHeadRow

RELATIVE = "contratos/acordo.xlsx"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _payload(text: str) -> dict:
    return {
        "source_type": "xlsx",
        "source_name": "acordo.xlsx",
        "pages": [
            {
                "page_number": 1,
                "status": "ok",
                "segments": [{"text": text, "kind": "text", "order": 0}],
            }
        ],
    }


class FakePlans:
    """Stands in for the stored text plan of each run."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}
        self.errors: dict[str, Exception] = {}

    def text_plan(self, run_id: str):
        if run_id in self.errors:
            raise self.errors[run_id]
        return self.payloads.get(run_id)


@pytest.fixture(autouse=True)
def _db():
    init_db()


@pytest.fixture
def plans(monkeypatch) -> FakePlans:
    fake = FakePlans()
    monkeypatch.setattr(ConversionRepository, "text_plan", lambda self, run_id: fake.text_plan(run_id))
    return fake


@pytest.fixture
def owner() -> str:
    return _uid("owner")


@pytest.fixture
def repo() -> KnowledgeRepository:
    return KnowledgeRepository()


@pytest.fixture
def collection(repo: KnowledgeRepository, owner: str) -> str:
    return repo.create_collection(owner_id=owner, name="Base")["collection_id"]


@pytest.fixture
def source_id() -> str:
    return _uid("src")


def _link_import(owner: str, source_id: str, relative_path: str, conversion_id: str) -> None:
    """Record that ``conversion_id`` was produced by importing ``relative_path``."""
    session = get_session_factory()()
    try:
        import_id = _uid("imp")
        session.add(
            SourceImportRow(
                id=import_id,
                owner_id=owner,
                source_id=source_id,
                source_version=1,
                idempotency_key=_uid("idem"),
                request_hash="0" * 64,
                definition_json={},
                options_json={},
            )
        )
        session.flush()  # parent row first: no relationship orders the inserts
        session.add(
            SourceImportItemRow(
                import_id=import_id,
                relative_path=relative_path,
                client_item_id=_uid("item"),
                conversion_id=conversion_id,
            )
        )
        session.commit()
    finally:
        session.close()


def _head(owner: str, collection_id: str, source_id: str, relative_path: str) -> dict | None:
    session = get_session_factory()()
    try:
        row = (
            session.query(SourceKnowledgeHeadRow)
            .filter_by(
                owner_id=owner,
                collection_id=collection_id,
                source_id=source_id,
                relative_path=relative_path,
            )
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "active_document_id": row.active_document_id,
            "active_run_id": row.active_run_id,
            "index_generation": row.index_generation,
            "source_missing": bool(row.source_missing),
        }
    finally:
        session.close()


def _index_version(
    *,
    plans: FakePlans,
    owner: str,
    collection_id: str,
    conversion_id: str,
    document_id: str,
    run_id: str,
    text: str,
) -> dict:
    plans.payloads[run_id] = _payload(text)
    return KnowledgeIndexer(KnowledgeRepository()).index_authorized(
        owner_id=owner,
        collection_id=collection_id,
        conversion_id=conversion_id,
        document_id=document_id,
        run_id=run_id,
        filename="acordo.xlsx",
    )


def _search(repo: KnowledgeRepository, owner: str, collection_id: str, query: str = "multa"):
    return retrieve(
        owner_id=owner, query=query, collection_ids=[collection_id], repository=repo
    )


# --------------------------------------------------------------- generations


def test_newest_version_replaces_previous_in_search(plans, repo, owner, collection, source_id):
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
        document_id="doc-v2", run_id="run-v2",
        text="Contrato novo prevê multa de vinte por cento.",
    )

    results = _search(repo, owner, collection)

    assert results
    assert {item.document_id for item in results} == {"doc-v2"}
    assert all("antigo" not in item.excerpt for item in results)
    head = _head(owner, collection, source_id, RELATIVE)
    assert head["active_document_id"] == "doc-v2"
    assert head["active_run_id"] == "run-v2"
    assert head["index_generation"] == 2


def test_failed_second_indexing_keeps_first_version_searchable(
    plans, repo, owner, collection, source_id
):
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )
    plans.errors["run-v2"] = RuntimeError("plano textual corrompido")

    with pytest.raises(RuntimeError):
        _index_version(
            plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
            document_id="doc-v2", run_id="run-v2", text="ignorado",
        )

    results = _search(repo, owner, collection)
    assert {item.document_id for item in results} == {"doc-v1"}
    head = _head(owner, collection, source_id, RELATIVE)
    assert head["active_document_id"] == "doc-v1"
    assert head["index_generation"] == 1


def test_failed_chunk_write_never_exposes_new_generation(
    plans, repo, owner, collection, source_id, monkeypatch
):
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )
    original = KnowledgeRepository.replace_document_chunks

    def failing_write(self, **kwargs):
        if kwargs["document_id"] == "doc-v2":
            raise RuntimeError("disco cheio")
        return original(self, **kwargs)

    monkeypatch.setattr(KnowledgeRepository, "replace_document_chunks", failing_write)
    with pytest.raises(RuntimeError):
        _index_version(
            plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
            document_id="doc-v2", run_id="run-v2",
            text="Contrato novo prevê multa de vinte por cento.",
        )

    results = _search(repo, owner, collection)
    assert {item.document_id for item in results} == {"doc-v1"}
    assert _head(owner, collection, source_id, RELATIVE)["active_document_id"] == "doc-v1"


def test_stale_observation_loses_promotion_and_is_reported_superseded(
    plans, repo, owner, collection, source_id, monkeypatch
):
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )
    # The second run observed a generation that is no longer current.
    monkeypatch.setattr(KnowledgeRepository, "head_generation", lambda self, *a, **k: 0)

    result = _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
        document_id="doc-v2", run_id="run-v2",
        text="Contrato novo prevê multa de vinte por cento.",
    )

    assert result["status"] == "superseded"
    assert {item.document_id for item in _search(repo, owner, collection)} == {"doc-v1"}
    head = _head(owner, collection, source_id, RELATIVE)
    assert head["active_document_id"] == "doc-v1"
    assert head["index_generation"] == 1


# ------------------------------------------------------------- vanished origin


def test_vanished_origin_flags_citations_without_hiding_content(
    plans, repo, owner, collection, source_id
):
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )

    flagged = repo.mark_source_missing(owner, source_id, [RELATIVE, "outro.xlsx"])

    assert flagged == 1
    results = _search(repo, owner, collection)
    assert results, "the last good generation stays searchable"
    assert all(item.source_missing for item in results)
    assert all(item.to_dict()["source_missing"] is True for item in results)
    assert "origem=indisponivel" in build_context(results)

    # A re-import of the file clears the flag on the promoted generation.
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
        document_id="doc-v2", run_id="run-v2",
        text="Contrato novo prevê multa de vinte por cento.",
    )
    results = _search(repo, owner, collection)
    assert {item.document_id for item in results} == {"doc-v2"}
    assert not any(item.source_missing for item in results)


def test_mark_source_missing_ignores_unknown_origin(repo, owner):
    assert repo.mark_source_missing(owner, "nao-existe", ["a.xlsx"]) == 0
    assert repo.mark_source_missing(owner, "nao-existe", []) == 0


# ------------------------------------------------------------- manual documents


def test_manual_documents_are_unaffected(plans, repo, owner, collection, source_id):
    manual_conv = _uid("conv")
    conv1, conv2 = _uid("conv"), _uid("conv")
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=manual_conv,
        document_id="doc-manual", run_id="run-manual",
        text="Anexo manual prevê multa de cinco por cento.",
    )
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv1,
        document_id="doc-v1", run_id="run-v1",
        text="Contrato antigo prevê multa de dez por cento.",
    )
    _index_version(
        plans=plans, owner=owner, collection_id=collection, conversion_id=conv2,
        document_id="doc-v2", run_id="run-v2",
        text="Contrato novo prevê multa de vinte por cento.",
    )

    results = _search(repo, owner, collection)

    assert {item.document_id for item in results} == {"doc-manual", "doc-v2"}
    manual = next(item for item in results if item.document_id == "doc-manual")
    assert manual.source_missing is False


# -------------------------------------------------------- compare-and-set


def test_promotion_with_stale_generation_only_one_wins(repo, owner, collection):
    origin = {"source_id": _uid("src"), "relative_path": "cas.xlsx"}
    first = repo.promote_source_head(
        owner_id=owner, collection_id=collection, origin=origin,
        document_id="doc-a", run_id="run-a", expected_generation=None,
    )
    assert first is True
    assert repo.head_generation(owner, collection, origin) == 1

    # Both writers observed generation 1 before either promoted.
    winner = repo.promote_source_head(
        owner_id=owner, collection_id=collection, origin=origin,
        document_id="doc-b", run_id="run-b", expected_generation=1,
    )
    loser = repo.promote_source_head(
        owner_id=owner, collection_id=collection, origin=origin,
        document_id="doc-c", run_id="run-c", expected_generation=1,
    )

    assert [winner, loser].count(True) == 1
    assert winner is True and loser is False
    assert repo.head_generation(owner, collection, origin) == 2
    head = _head(owner, collection, origin["source_id"], "cas.xlsx")
    assert head["active_document_id"] == "doc-b"
    assert head["index_generation"] == 2


def test_create_only_promotion_never_overwrites_an_existing_head(repo, owner, collection):
    origin = {"source_id": _uid("src"), "relative_path": "create.xlsx"}
    assert repo.promote_source_head(
        owner_id=owner, collection_id=collection, origin=origin,
        document_id="doc-a", run_id="run-a", expected_generation=None,
    )

    late = repo.promote_source_head(
        owner_id=owner, collection_id=collection, origin=origin,
        document_id="doc-z", run_id="run-z", expected_generation=None,
    )

    assert late is False
    head = _head(owner, collection, origin["source_id"], "create.xlsx")
    assert head["active_document_id"] == "doc-a"
    assert head["index_generation"] == 1


# ----------------------------------------------------------- job outcomes


def _legacy_job(repo: KnowledgeRepository, owner: str, collection_id: str, conversion_ids: list[str]) -> str:
    job = repo.create_job(
        owner_id=owner,
        collection_id=collection_id,
        source={
            "conversion_ids": conversion_ids,
            "source_paths": [],
            "scope": "documents",
            "dispatch": KnowledgeIndexer.LEGACY_DISPATCH,
        },
    )
    return job["job_id"]


def test_legacy_job_with_some_failed_documents_reports_partial(repo, owner, collection):
    indexer = KnowledgeIndexer(repo)
    job_id = _legacy_job(repo, owner, collection, ["ok-1", "bad-1"])

    def fake(owner_id, collection_id, conversion_id):  # noqa: ARG001
        if conversion_id == "bad-1":
            return {"document_id": "d-bad", "status": "skipped", "chunks": 0}
        return {"document_id": "d-ok", "status": "indexed", "chunks": 2}

    indexer._index_conversion = fake
    indexer.run_job(job_id, owner)

    job = repo.get_job(owner, job_id)
    assert job["status"] == "partial"
    assert job["error"]["code"] == "INDEX_PARTIAL"
    assert job["stats"]["chunks"] == 2
    assert job["stats"]["failed"] == 1


def test_legacy_job_with_no_indexed_documents_reports_failed(repo, owner, collection):
    indexer = KnowledgeIndexer(repo)
    job_id = _legacy_job(repo, owner, collection, ["bad-1"])
    indexer._index_conversion = lambda owner_id, collection_id, conversion_id: {  # noqa: ARG005
        "document_id": "d-bad", "status": "skipped", "chunks": 0,
    }

    indexer.run_job(job_id, owner)

    assert repo.get_job(owner, job_id)["status"] == "failed"


def test_lease_job_failed_update_keeps_previous_version_searchable(
    plans, repo, owner, collection, source_id
):
    """Through the durable runner: a failing second version leaves v1 active."""
    doc1, doc2 = _uid("doc"), _uid("doc")
    conv1, conv2 = _uid("conv"), _uid("conv")
    run1, run2 = _uid("run"), _uid("run")
    session = get_session_factory()()
    try:
        for doc, conv, run in ((doc1, conv1, run1), (doc2, conv2, run2)):
            session.add(
                PublicDocumentRow(
                    id=doc, filename="acordo.xlsx", sha256="0" * 64,
                    storage_key=f"k/{doc}", owner_id=owner,
                )
            )
            session.flush()  # document row first: the conversion references it
            session.add(
                PublicConversionRow(
                    id=conv, document_id=doc, owner_id=owner,
                    status="completed", active_run_id=run, options_json={},
                )
            )
        session.commit()
    finally:
        session.close()
    _link_import(owner, source_id, RELATIVE, conv1)
    _link_import(owner, source_id, RELATIVE, conv2)
    plans.payloads[run1] = _payload("Contrato antigo prevê multa de dez por cento.")
    plans.payloads[run2] = _payload("Contrato novo prevê multa de vinte por cento.")
    runner = KnowledgeJobRunner()

    first = runner.create(owner_id=owner, collection_id=collection, conversion_ids=[conv1])
    assert runner.dispatch(first["job_id"], owner)["status"] == "completed"
    assert {item.document_id for item in _search(repo, owner, collection)} == {doc1}

    plans.errors[run2] = RuntimeError("plano textual corrompido")
    second = runner.create(owner_id=owner, collection_id=collection, conversion_ids=[conv2])
    assert runner.dispatch(second["job_id"], owner)["status"] == "failed"

    assert {item.document_id for item in _search(repo, owner, collection)} == {doc1}
    assert _head(owner, collection, source_id, RELATIVE)["active_document_id"] == doc1
