"""Page checkpoints: resume, invalidation and corruption handling."""

from __future__ import annotations

import pytest

from audit.events import AuditRecorder
from engine.pipeline import ConversionPipeline, PipelineOptions, extraction_fingerprint
from repositories.checkpoint_repository import (
    STAGE_EXTRACT,
    CheckpointKey,
    CheckpointRepository,
    options_hash,
)
from storage import get_storage
from version import ENGINE_VERSION


def _key(sha: str, *, ocr: bool = False, engine: str = ENGINE_VERSION) -> CheckpointKey:
    """Uses the pipeline's own fingerprint, so the test cannot drift from it."""
    return CheckpointKey(
        source_sha256=sha,
        engine_version=engine,
        options_hash=options_hash(extraction_fingerprint(PipelineOptions(ocr_enabled=ocr))),
    )


class _CountingPipeline(ConversionPipeline):
    """Counts how many pages actually went through extraction."""

    processed = 0

    def _process_page(self, *args, **kwargs):  # type: ignore[override]
        type(self).processed += 1
        return super()._process_page(*args, **kwargs)


def _run(tmp_path, fixture, run_id: str, *, resume: bool, ocr: bool = False):
    recorder = AuditRecorder(run_id=run_id)
    options = PipelineOptions(ocr_enabled=ocr)
    _CountingPipeline.processed = 0
    pipeline = _CountingPipeline(
        pdf_path=fixture,
        run_id=run_id,
        conversion_id=f"conv_{run_id}",
        recorder=recorder,
        options=options,
        resume=resume,
    )
    result = pipeline.run()
    return result, recorder, _CountingPipeline.processed


def test_a_fresh_run_writes_a_checkpoint_per_page(pdf_paths, tmp_path):
    fixture = pdf_paths["multipage_table"]
    run_id = "run_ckpt_fresh"
    result, _recorder, processed = _run(tmp_path, fixture, run_id, resume=False)

    total = len(result.ir.pages)
    assert total >= 2
    assert processed == total

    repo = CheckpointRepository()
    rows = repo.existing_pages(run_id=run_id, key=_key(result.ir.sha256))
    assert len(rows) == total


def test_resume_reloads_pages_without_reprocessing(pdf_paths, tmp_path):
    fixture = pdf_paths["multipage_table"]
    run_id = "run_ckpt_resume"

    first, _r1, processed_first = _run(tmp_path, fixture, run_id, resume=False)
    assert processed_first == len(first.ir.pages)

    second, _r2, processed_second = _run(tmp_path, fixture, run_id, resume=True)
    assert processed_second == 0, "a resumed run must not reprocess a single page"
    assert [p.page_number for p in second.ir.pages] == [p.page_number for p in first.ir.pages]

    # The restored pages keep their text and provenance.
    for original, restored in zip(first.ir.pages, second.ir.pages):
        assert [b.text for b in restored.blocks] == [b.text for b in original.blocks]
        assert len(restored.tokens) == len(original.tokens)


def test_a_different_run_id_never_reuses_checkpoints(pdf_paths, tmp_path):
    fixture = pdf_paths["multipage_table"]
    first, _r1, _ = _run(tmp_path, fixture, "run_ckpt_a", resume=False)
    second, _r2, processed = _run(tmp_path, fixture, "run_ckpt_b", resume=True)
    assert processed == len(second.ir.pages), "checkpoints are scoped to their run"
    assert first.ir.sha256 == second.ir.sha256


def test_different_options_invalidate_the_checkpoint(pdf_paths, tmp_path):
    fixture = pdf_paths["multipage_table"]
    run_id = "run_ckpt_options"
    first, _r1, _ = _run(tmp_path, fixture, run_id, resume=False, ocr=False)

    repo = CheckpointRepository()
    assert repo.existing_pages(run_id=run_id, key=_key(first.ir.sha256, ocr=False))
    # Same document, different extraction options: nothing may be reused.
    assert not repo.existing_pages(run_id=run_id, key=_key(first.ir.sha256, ocr=True))


def test_different_engine_version_invalidates_the_checkpoint(pdf_paths, tmp_path):
    fixture = pdf_paths["multipage_table"]
    run_id = "run_ckpt_engine"
    first, _r1, _ = _run(tmp_path, fixture, run_id, resume=False)

    repo = CheckpointRepository()
    assert not repo.existing_pages(
        run_id=run_id, key=_key(first.ir.sha256, engine="another-engine")
    )


def test_a_corrupted_artifact_is_reprocessed_not_trusted(pdf_paths, tmp_path):
    fixture = pdf_paths["mixed_report"]
    run_id = "run_ckpt_corrupt"
    result, _r, _ = _run(tmp_path, fixture, run_id, resume=False)

    repo = CheckpointRepository()
    key = _key(result.ir.sha256)
    rows = repo.existing_pages(run_id=run_id, key=key)
    assert rows

    # Corrupt the stored artifact behind the checkpoint's back.
    first_page = min(rows)
    get_storage().put_bytes(rows[first_page].artifact_key, b"not a gzip artifact", "application/gzip")

    assert repo.load_page(run_id=run_id, page_number=first_page, key=key) is None
    # Other pages are unaffected.
    other = [number for number in rows if number != first_page]
    if other:
        assert repo.load_page(run_id=run_id, page_number=other[0], key=key) is not None


def test_a_failed_page_is_never_restored(pdf_paths, tmp_path):
    from models.document import PageResult

    repo = CheckpointRepository()
    run_id = "run_ckpt_failed"
    key = CheckpointKey(
        source_sha256="a" * 64,
        engine_version=ENGINE_VERSION,
        options_hash=options_hash({}),
    )
    failed = PageResult(page_number=9, width=0, height=0, classification="empty", status="failed")
    repo.save_page(run_id=run_id, page=failed, key=key, stage=STAGE_EXTRACT)

    # A failed page must be retried, not replayed as a success.
    assert repo.load_page(run_id=run_id, page_number=9, key=key) is None


def test_options_hash_is_stable_and_order_independent():
    assert options_hash({"a": 1, "b": 2}) == options_hash({"b": 2, "a": 1})
    assert options_hash({"a": 1}) != options_hash({"a": 2})


def test_phase_cursor_roundtrip():
    repo = CheckpointRepository()
    repo.set_phase_cursor(run_id="run_ckpt_cursor", phase="structure", cursor="page:25", event_sequence=7)
    row = repo.get_phase_cursor(run_id="run_ckpt_cursor", phase="structure")
    assert row is not None
    assert row.cursor == "page:25"
    assert row.last_event_sequence == 7


def test_losing_writer_does_not_overwrite_the_winning_artifact():
    # AU2-27: artifact keys are content-addressed, so a second writer for the
    # same page cannot replace the bytes the winning row points to.
    import hashlib

    repo = CheckpointRepository()
    run_id = "run_ckpt_artifact_race"
    key = CheckpointKey(
        source_sha256="c" * 64,
        engine_version=ENGINE_VERSION,
        options_hash=options_hash({}),
    )
    from models.document import PageResult

    winner = PageResult(page_number=4, width=10, height=10, native_chars=5)
    loser = PageResult(page_number=4, width=10, height=10, native_chars=99)
    winner_ref = repo.save_page(run_id=run_id, page=winner, key=key)
    repo.save_page(run_id=run_id, page=loser, key=key)

    stored = get_storage().get_bytes(winner_ref.artifact_key)
    assert hashlib.sha256(stored).hexdigest() == winner_ref.artifact_sha256


def test_phase_cursor_insert_race_updates_the_winning_row(monkeypatch):
    # AU2-28: when another writer inserts the same (run, phase) first, the
    # cursor write must update that row instead of failing with IntegrityError.
    import repositories.checkpoint_repository as module
    from models.db import RunCheckpointRow, get_session_factory

    repo = CheckpointRepository()
    run_id = "run_ckpt_cursor_race"
    repo.set_phase_cursor(run_id=run_id, phase="structure", cursor="page:1", event_sequence=1)

    pending = {"blind": True}

    class _BlindFirstLookup:
        """Session whose first RunCheckpointRow lookup misses (simulated race)."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", None) or [{}]
            if pending["blind"] and descriptions[0].get("entity") is RunCheckpointRow:
                pending["blind"] = False

                class _Miss:
                    def scalar_one_or_none(self):
                        return None

                return _Miss()
            return self._inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        module, "get_session_factory", lambda: (lambda: _BlindFirstLookup(get_session_factory()()))
    )
    repo.set_phase_cursor(run_id=run_id, phase="structure", cursor="page:9", event_sequence=4)
    monkeypatch.undo()

    row = repo.get_phase_cursor(run_id=run_id, phase="structure")
    assert row is not None
    assert row.cursor == "page:9"
    assert row.last_event_sequence == 4
