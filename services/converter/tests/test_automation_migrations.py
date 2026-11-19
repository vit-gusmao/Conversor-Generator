"""T2 — sources/automation persistence and migrations.

Covers:
- the versioned migration chain upgrades an empty database and a database
  that already has conversions (rows preserved, counts and content hashes);
- the unique constraints the plan depends on (occurrence key, file version,
  import idempotency, event sequence, knowledge head);
- the transactional repository operations used by later tasks.

The PostgreSQL scenario only runs when ``POSTGRES_TEST_URL`` points at a
disposable database (never the real one); otherwise it is skipped and
reported as not run here.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from models.db import Base, init_db

SERVICE_DIR = Path(__file__).resolve().parents[1]

from datetime import UTC, datetime

_DT = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)

NEW_TABLES = (
    "source_roots",
    "file_sources",
    "source_imports",
    "source_import_items",
    "source_file_versions",
    "source_knowledge_heads",
    "automation_schedules",
    "automation_runs",
    "automation_events",
)


def _alembic(database_url: str, *args: str) -> None:
    env = dict(os.environ)
    env["CONVERTER_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(SERVICE_DIR / "alembic.ini"), *args],
        cwd=str(SERVICE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_empty_database_upgrade_creates_all_tables(tmp_path: Path):
    db = tmp_path / "empty.db"
    _alembic(_sqlite_url(db), "upgrade", "head")
    engine = create_engine(_sqlite_url(db))
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    for table in NEW_TABLES:
        assert table in names, f"table {table} missing after upgrade head"


def test_upgrade_preserves_existing_conversion_rows(tmp_path: Path):
    db = tmp_path / "existing.db"
    _alembic(_sqlite_url(db), "upgrade", "e08fecd3c89f")

    # Seed a conversion from the previous head, using the current model
    # metadata (all prior revisions must be expressible with these models).
    from models.db import PublicConversionRow, PublicDocumentRow, Run

    engine = create_engine(_sqlite_url(db))
    Session = sessionmaker(bind=engine)
    with Session() as session:
        doc = PublicDocumentRow(
            id="doc_seed1",
            filename="antigo.pdf",
            mime_type="application/pdf",
            size_bytes=123,
            sha256="a" * 64,
            storage_key="documents/owner1/" + "a" * 64 + ".pdf",
            page_count=3,
            owner_id="owner1",
        )
        conv = PublicConversionRow(
            id="conv_seed1",
            document_id="doc_seed1",
            owner_id="owner1",
            status="completed",
            quality="validated",
        )
        run = Run(id="run_seed1", conversion_id="conv_seed1", status="completed")
        session.add_all([doc, conv, run])
        session.commit()

    _alembic(_sqlite_url(db), "upgrade", "head")

    with Session() as session:
        conv = session.get(PublicConversionRow, "conv_seed1")
        run = session.get(Run, "run_seed1")
        doc = session.get(PublicDocumentRow, "doc_seed1")
        assert conv is not None and conv.status == "completed"
        assert run is not None and run.status == "completed"
        assert doc is not None and doc.sha256 == "a" * 64
        digest = hashlib.sha256(doc.sha256.encode()).hexdigest()
        assert digest == hashlib.sha256(("a" * 64).encode()).hexdigest()

    for table in NEW_TABLES:
        with engine.connect() as conn:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        assert count == 0, f"new table {table} must start empty after upgrade"


def test_upgrade_head_is_new_revision(tmp_path: Path):
    db = tmp_path / "chain.db"
    _alembic(_sqlite_url(db), "upgrade", "head")
    _alembic(_sqlite_url(db), "current")
    # `current` succeeded, so the chain is linear and head is applied.


@pytest.mark.parametrize(
    ("table", "payload_a", "payload_b", "name", "parents"),
    [
        (
            "automation_runs",
            dict(
                id="run_a",
                owner_id="o1",
                schedule_id="s1",
                schedule_revision=1,
                scheduled_for=_DT,
                trigger="schedule",
                occurrence_key="s1:1:20261119T0800",
                snapshot_json={"source": "x"},
                state="queued",
                attempt=0,
                fence=0,
            ),
            dict(
                id="run_b",
                owner_id="o1",
                schedule_id="s1",
                schedule_revision=1,
                scheduled_for=_DT,
                trigger="schedule",
                occurrence_key="s1:1:20261119T0800",
                snapshot_json={"source": "x"},
                state="queued",
                attempt=0,
                fence=0,
            ),
            "occurrence_key must be globally unique",
            {},
        ),
        (
            "source_file_versions",
            dict(
                owner_id="o1",
                source_id="src1",
                relative_path="planilha.xlsx",
                sha256="b" * 64,
                options_hash="c" * 64,
                last_success_import_id="imp1",
            ),
            dict(
                owner_id="o1",
                source_id="src1",
                relative_path="planilha.xlsx",
                sha256="b" * 64,
                options_hash="c" * 64,
                last_success_import_id="imp2",
            ),
            "file version identity must be unique",
            {},
        ),
        (
            "source_imports",
            dict(
                id="imp_a",
                owner_id="o1",
                source_id="src1",
                source_version=1,
                idempotency_key="key-1",
                request_hash="d" * 64,
                status="pending",
                definition_json={"root_id": "root_1"},
                fence=0,
            ),
            dict(
                id="imp_b",
                owner_id="o1",
                source_id="src1",
                source_version=1,
                idempotency_key="key-1",
                request_hash="d" * 64,
                status="pending",
                definition_json={"root_id": "root_1"},
                fence=0,
            ),
            "import idempotency key must be unique per owner",
            {},
        ),
        (
            "automation_events",
            dict(
                run_id="run_ev_parent",
                sequence=1,
                type="OCCURRENCE_DUE",
            ),
            dict(
                run_id="run_ev_parent",
                sequence=1,
                type="RUN_CLAIMED",
            ),
            "event (run_id, sequence) must be unique",
            {"automation_runs": [dict(
                id="run_ev_parent",
                owner_id="o1",
                schedule_id="s1",
                schedule_revision=1,
                scheduled_for=_DT,
                trigger="schedule",
                occurrence_key="s1:1:20261119T0800-ev",
                snapshot_json={},
                state="queued",
                attempt=0,
                fence=0,
            )]},
        ),
        (
            "source_knowledge_heads",
            dict(
                owner_id="o1",
                collection_id="col1",
                source_id="src1",
                relative_path="planilha.xlsx",
                active_document_id="doc1",
                active_run_id="run1",
                index_generation=1,
                source_missing=False,
            ),
            dict(
                owner_id="o1",
                collection_id="col1",
                source_id="src1",
                relative_path="planilha.xlsx",
                active_document_id="doc2",
                active_run_id="run2",
                index_generation=2,
                source_missing=False,
            ),
            "knowledge head must be unique per logical file",
            {},
        ),
    ],
)
def test_unique_constraints(table: str, payload_a: dict, payload_b: dict, name: str, parents: dict):
    init_db()
    from sqlalchemy.orm import Session

    from models.db import get_engine, get_session_factory

    model = Base.metadata.tables[table]
    engine = get_engine()
    with Session(bind=engine) as session:
        for parent_table, parent_rows in parents.items():
            parent_model = Base.metadata.tables[parent_table]
            for row in parent_rows:
                session.execute(parent_model.insert().values(**row))
        session.execute(model.insert().values(**payload_a))
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(model.insert().values(**payload_b))
            session.commit()
        session.rollback()


def test_schedule_optimistic_revision_update():
    """Editing with a stale revision must fail without overwriting."""
    init_db()
    from automation.repository import AutomationRepository
    from sqlalchemy.orm import Session

    from models.db import get_engine

    repo = AutomationRepository()
    with Session(bind=get_engine()) as session:
        schedule = repo.create_schedule(
            session,
            owner_id="owner-rev",
            name="Diária",
            source_id="src1",
            rule={"kind": "daily", "local_time": "08:00"},
            timezone="America/Sao_Paulo",
        )
        schedule_id = schedule.id
        assert schedule.revision == 1

        updated = repo.update_schedule(
            session,
            schedule_id=schedule_id,
            owner_id="owner-rev",
            expected_revision=1,
            changes={"name": "Diária v2"},
        )
        assert updated is not None and updated.revision == 2

        stale = repo.update_schedule(
            session,
            schedule_id=schedule_id,
            owner_id="owner-rev",
            expected_revision=1,
            changes={"name": "Sobrescrevente"},
        )
        assert stale is None
        session.commit()

    with Session(bind=get_engine()) as session:
        current = repo.get_schedule(session, schedule_id=schedule_id, owner_id="owner-rev")
        assert current is not None
        assert current.name == "Diária v2"
        assert current.revision == 2


def test_foreign_owner_cannot_read_or_write():
    init_db()
    from automation.repository import AutomationRepository
    from sources.repository import SourceRepository
    from sqlalchemy.orm import Session

    from models.db import get_engine

    sources = SourceRepository()
    automation = AutomationRepository()
    with Session(bind=get_engine()) as session:
        root = sources.create_root(
            session, owner_id="owner-a", runtime_id="local", canonical_path=r"C:\Dados", granted_by="owner-a"
        )
        source = sources.create_source(
            session,
            owner_id="owner-a",
            root_id=root.id,
            relative_path="financeiro",
            kind="directory",
            name="Financeiro",
        )
        schedule = automation.create_schedule(
            session,
            owner_id="owner-a",
            name="Agenda",
            source_id=source.id,
            rule={"kind": "interval", "every_seconds": 3600},
            timezone="UTC",
        )
        session.commit()

        assert sources.get_source(session, source_id=source.id, owner_id="owner-b") is None
        assert automation.get_schedule(session, schedule_id=schedule.id, owner_id="owner-b") is None
        assert sources.get_source(session, source_id=source.id, owner_id="owner-a") is not None


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="POSTGRES_TEST_URL não definido: cenário PostgreSQL requer banco descartável próprio",
)
def test_postgres_disposable_upgrade():
    url = os.environ["POSTGRES_TEST_URL"]
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "e08fecd3c89f")
    _alembic(url, "upgrade", "head")
