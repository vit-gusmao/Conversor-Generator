"""RuntimePaths: the single source of mutable/readable locations (T11).

Resolution order for every directory is explicit environment first, then the
repository layout (developer runs). A packaged process gets absolute paths
from ``desktop_entry`` before any module imports ``settings`` — the singleton
is created on first import, so order matters and is asserted in tests.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    app_data_dir: Path
    work_dir: Path
    log_dir: Path
    storage_dir: Path
    web_dist_dir: Path
    backup_dir: Path
    identity_dir: Path

    # ------------------------------------------------------------------ build
    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        """Resolve paths from env vars, falling back to the repository layout.

        ``APP_DATA_DIR`` is required for packaged runs; when absent the
        developer layout under ``services/converter/data`` is used.
        """
        repo_root = _repo_root()
        app_data = os.environ.get("APP_DATA_DIR")
        if app_data:
            base = Path(app_data)
        else:
            base = repo_root / "services" / "converter" / "data"

        work = Path(os.environ["WORK_DIR"]) if os.environ.get("WORK_DIR") else base / "work"
        logs = Path(os.environ["LOG_DIR"]) if os.environ.get("LOG_DIR") else base / "logs"
        storage_env = os.environ.get("STORAGE_LOCAL_ROOT")
        storage = Path(storage_env) if storage_env else base / "storage"
        web_env = os.environ.get("WEB_DIST_DIR")
        if web_env:
            web_dist = Path(web_env)
        else:
            web_dist = repo_root / "apps" / "web" / "dist"

        return cls(
            app_data_dir=base,
            work_dir=work,
            log_dir=logs,
            storage_dir=storage,
            web_dist_dir=web_dist,
            backup_dir=base / "backups",
            identity_dir=base / "identity",
        )

    # -------------------------------------------------------------- apply
    def apply(self) -> None:
        """Export the resolved paths so `config.settings` uses them.

        Must run BEFORE importing anything that materializes `settings`
        (config has an lru_cache singleton created at import time).
        """
        os.environ["APP_DATA_DIR"] = str(self.app_data_dir)
        os.environ["WORK_DIR"] = str(self.work_dir)
        os.environ["LOG_DIR"] = str(self.log_dir)
        os.environ["STORAGE_LOCAL_ROOT"] = str(self.storage_dir)
        os.environ["WEB_DIST_DIR"] = str(self.web_dist_dir)
        # Relative SQLite URLs resolve against SERVICE_DIR; an explicit
        # absolute URL keeps the packaged database inside APP_DATA_DIR.
        if "CONVERTER_DATABASE_URL" not in os.environ:
            os.environ["CONVERTER_DATABASE_URL"] = (
                f"sqlite:///{(self.app_data_dir / 'converter.db').as_posix()}"
            )

    def ensure(self) -> None:
        """Create every mutable directory (never the resources directory)."""
        for directory in (
            self.app_data_dir,
            self.work_dir,
            self.log_dir,
            self.storage_dir,
            self.backup_dir,
            self.identity_dir,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- checks
    @property
    def web_dist_available(self) -> bool:
        return (self.web_dist_dir / "index.html").exists()

    def assert_isolated_from_resources(self) -> None:
        """Guard: mutable dirs must not live inside installed resources/ASAR."""
        resources = os.environ.get("RESOURCES_DIR")
        if not resources:
            return
        resource_root = Path(resources).resolve()
        for directory in (self.app_data_dir, self.work_dir, self.log_dir, self.storage_dir):
            resolved = Path(directory).resolve()
            if resolved == resource_root or resource_root in resolved.parents:
                raise RuntimeError(
                    f"Diretório mutável dentro dos recursos instalados: {resolved}. "
                    "Use APP_DATA_DIR/WORK_DIR no perfil do usuário."
                )


def _repo_root() -> Path:
    """Repository root for a source checkout; the app dir when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve()
    # services/converter/runtime/paths.py → repo root is parents[3]
    return here.parents[3]


__all__ = ["RuntimePaths"]
