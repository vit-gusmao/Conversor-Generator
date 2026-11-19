"""Startup schema management for the packaged desktop app (T11).

The frozen build does not ship Alembic (it is an optional `migrate` extra and
`migrations/` is not in `packaging/converter.spec`), so the startup path uses
the models directly and records the head revision itself:

1. take an exclusive lock at ``<app_data>/.migrate.lock`` so only one process
   migrates; a second process waits, then fails instead of racing;
2. read the revision the database records; refuse one this build does not know
   (it was written by a newer build) before anything is changed;
3. when the schema is about to change, copy the database with SQLite's online
   backup API into ``<app_data>/backups/converter-<UTC>.db``, keeping the newest
   ``BACKUP_KEEP`` copies;
4. run ``init_db`` (create missing tables, add missing columns and indexes);
5. write ``HEAD_REVISION`` into ``alembic_version`` so that Alembic tooling and
   later builds see a consistent history.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: The revision every current model set corresponds to (`alembic heads`).
HEAD_REVISION = "d5e0a7c31b92"
#: Every revision this build understands, oldest first. A test keeps this equal
#: to the revisions in `migrations/versions`.
KNOWN_REVISIONS: tuple[str, ...] = (
    "a95827baa7d8",
    "c4f1a2b7d910",
    "6d13f972f6c5",
    "e08fecd3c89f",
    "f2a8c61b9e73",
    "b7c4d2e91f38",
    "d5e0a7c31b92",
)
BACKUP_KEEP = 5
LOCK_NAME = ".migrate.lock"
DEFAULT_LOCK_TIMEOUT = 30.0


class SchemaTooNewError(RuntimeError):
    """The database records a revision this build does not know. Nothing was changed."""


class MigrationLockTimeout(RuntimeError):
    """Another process held the migration lock for longer than the timeout."""


@dataclass(frozen=True)
class MigrationReport:
    revision_before: str | None
    revision_after: str
    backup: Path | None
    changes: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Exclusive lock
# --------------------------------------------------------------------------- #
@contextmanager
def exclusive_lock(
    path: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll_seconds: float = 0.1,
) -> Iterator[None]:
    """Holds an exclusive OS file lock on ``path`` for the duration of the block."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - closed in the finally below
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _lock_handle(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MigrationLockTimeout(
                        f"outro processo está migrando o banco de dados ({path})"
                    ) from None
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            _unlock_handle(handle)
    finally:
        handle.close()


def _lock_handle(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - exercised on POSIX CI only
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI only
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
def backup_sqlite(
    database: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
    keep: int = BACKUP_KEEP,
) -> Path:
    """Copies a SQLite database consistently and prunes older backups.

    The online backup API reads through a single consistent snapshot, so a
    concurrent writer cannot leave a half-written copy behind.
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"converter-{stamp}.db"

    source = sqlite3.connect(str(database))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    _prune_backups(backup_dir, keep)
    return target


def _prune_backups(backup_dir: Path, keep: int) -> None:
    # The UTC timestamp is fixed-width, so lexical order is chronological.
    backups = sorted(backup_dir.glob("converter-*.db"))
    stale = backups[:-keep] if keep > 0 else backups
    for old in stale:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Revision bookkeeping
# --------------------------------------------------------------------------- #
def read_revision(engine) -> str | None:
    """The recorded revision, or None when the database was never stamped.

    More than one row is returned as a joined string, which is not a known
    revision and therefore refused.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    if not sa_inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as conn:
        rows = [str(row[0]) for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
    if not rows:
        return None
    return rows[0] if len(rows) == 1 else ",".join(rows)


def _stamp_head(engine) -> None:
    from sqlalchemy import Column, MetaData, String, Table

    meta = MetaData()
    version = Table(
        "alembic_version",
        meta,
        Column("version_num", String(32), primary_key=True, nullable=False),
    )
    meta.create_all(engine, tables=[version])
    with engine.begin() as conn:
        conn.execute(version.delete())
        conn.execute(version.insert().values(version_num=HEAD_REVISION))


def _sqlite_file(engine) -> Path | None:
    url = engine.url
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_startup_migrations(
    app_data_dir: Path,
    engine,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> MigrationReport:
    """Brings the database behind ``engine`` to ``HEAD_REVISION`` safely.

    Raises :class:`SchemaTooNewError` (database untouched) or
    :class:`MigrationLockTimeout` (database untouched) instead of migrating.
    """
    from models.db import init_db, schema_gaps

    app_data_dir = Path(app_data_dir)
    database = _sqlite_file(engine)
    existed = database is not None and database.exists()

    with exclusive_lock(app_data_dir / LOCK_NAME, timeout=lock_timeout):
        before = read_revision(engine)
        if before is not None and before not in KNOWN_REVISIONS:
            raise SchemaTooNewError(
                f"O banco de dados foi criado por uma versão mais nova do aplicativo "
                f"(revisão {before}). Atualize o aplicativo; nenhuma alteração foi feita."
            )

        changes = list(schema_gaps(engine))
        if before != HEAD_REVISION:
            changes.append(f"revisão {before or 'ausente'} -> {HEAD_REVISION}")

        backup: Path | None = None
        if changes and existed and database is not None:
            backup = backup_sqlite(database, app_data_dir / "backups")

        init_db(engine)
        _stamp_head(engine)

    return MigrationReport(
        revision_before=before,
        revision_after=HEAD_REVISION,
        backup=backup,
        changes=tuple(changes),
    )


__all__ = [
    "BACKUP_KEEP",
    "HEAD_REVISION",
    "KNOWN_REVISIONS",
    "MigrationLockTimeout",
    "MigrationReport",
    "SchemaTooNewError",
    "backup_sqlite",
    "exclusive_lock",
    "read_revision",
    "run_startup_migrations",
]
