"""T11 — explicit runtime directories for the packaged app."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from runtime.paths import RuntimePaths

SERVICE_DIR = Path(__file__).resolve().parents[1]


def test_paths_fall_back_to_the_repository_layout(monkeypatch):
    for key in ("APP_DATA_DIR", "WORK_DIR", "LOG_DIR", "STORAGE_LOCAL_ROOT", "WEB_DIST_DIR"):
        monkeypatch.delenv(key, raising=False)
    paths = RuntimePaths.from_environment()
    assert paths.app_data_dir.name == "data"
    assert paths.work_dir.name == "work"
    assert paths.web_dist_dir.name == "dist"
    assert paths.storage_dir.name == "storage"


def test_explicit_directories_win(tmp_path: Path, monkeypatch):
    app_data = tmp_path / "Conversor Generator"  # spaces are fine
    monkeypatch.setenv("APP_DATA_DIR", str(app_data))
    monkeypatch.setenv("WORK_DIR", str(app_data / "work"))
    monkeypatch.setenv("LOG_DIR", str(app_data / "logs"))
    monkeypatch.delenv("STORAGE_LOCAL_ROOT", raising=False)
    paths = RuntimePaths.from_environment()
    assert paths.app_data_dir == app_data
    assert paths.work_dir == app_data / "work"
    assert paths.storage_dir == app_data / "storage"


def test_apply_exports_absolute_paths_and_database(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "user data"))
    monkeypatch.delenv("CONVERTER_DATABASE_URL", raising=False)
    paths = RuntimePaths.from_environment()
    paths.apply()
    assert os.environ["APP_DATA_DIR"] == str(tmp_path / "user data")
    assert os.environ["CONVERTER_DATABASE_URL"].startswith("sqlite:///")
    assert "converter.db" in os.environ["CONVERTER_DATABASE_URL"]


def test_ensure_creates_every_mutable_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "dados"))
    monkeypatch.delenv("WORK_DIR", raising=False)
    monkeypatch.delenv("LOG_DIR", raising=False)
    paths = RuntimePaths.from_environment()
    paths.ensure()
    for directory in (
        paths.app_data_dir,
        paths.work_dir,
        paths.log_dir,
        paths.storage_dir,
        paths.backup_dir,
        paths.identity_dir,
    ):
        assert Path(directory).is_dir()


def test_mutable_dirs_inside_resources_are_refused(tmp_path: Path, monkeypatch):
    resources = tmp_path / "resources"
    resources.mkdir()
    monkeypatch.setenv("RESOURCES_DIR", str(resources))
    monkeypatch.setenv("APP_DATA_DIR", str(resources / "data"))
    paths = RuntimePaths.from_environment()
    with pytest.raises(RuntimeError):
        paths.assert_isolated_from_resources()

    # A user directory is accepted.
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "user"))
    RuntimePaths.from_environment().assert_isolated_from_resources()


def test_web_dist_flag_reflects_presence(tmp_path: Path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setenv("WEB_DIST_DIR", str(dist))
    paths = RuntimePaths.from_environment()
    assert paths.web_dist_available is False
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert paths.web_dist_available is True


def test_desktop_entry_prepares_runtime_from_a_clean_cwd(tmp_path: Path):
    """The entry point works from outside the repo with userData-like paths.

    Runs in a subprocess so the settings singleton is created fresh with the
    environment the supervisor passes (exactly the packaged startup order).
    """
    app_data = tmp_path / "Conversor Generator" / "userData"
    env = dict(os.environ)
    env.update(
        {
            "APP_DATA_DIR": str(app_data),
            "WORK_DIR": str(app_data / "work"),
            "LOG_DIR": str(app_data / "logs"),
            "STORAGE_LOCAL_ROOT": str(app_data / "storage"),
            "WEB_DIST_DIR": str(tmp_path / "dist"),
            "CONVERTER_DATABASE_URL": f"sqlite:///{(app_data / 'converter.db').as_posix()}",
            "SERVICE_DIR": str(SERVICE_DIR),
            "CONVERTER_OCR_ENABLED": "false",
            "CONVERTER_QUEUE": "inline",
            # The supervisor always supplies these; the entry refuses without them.
            "CONVERTER_INTERNAL_TOKEN": "i" * 43,
            "DESKTOP_BOOTSTRAP_SECRET": "b" * 43,
            "JWT_SECRET": "j" * 96,
        }
    )
    script = textwrap.dedent(
        """
        import os
        import sys
        sys.path.insert(0, os.environ["SERVICE_DIR"])
        from desktop_entry import main
        sys.exit(main(["--no-serve"]))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(tmp_path),  # outside the repository
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "CONVERTER_READY" in result.stdout
    assert (app_data / "converter.db").exists()
    assert (app_data / "storage").is_dir()
    assert (app_data / "work").is_dir()


def test_readiness_publishes_the_bound_port(tmp_path: Path):
    from desktop_entry import publish_readiness

    target = tmp_path / "ready.json"
    publish_readiness(54321, "boot-1", "1.0.0", target)
    payload = target.read_text(encoding="utf-8")
    assert '"port": 54321' in payload
    assert "boot-1" in payload


def test_free_port_is_assigned_by_the_os():
    from desktop_entry import _free_loopback_port

    port = _free_loopback_port()
    assert 1024 < port < 65536


def test_settings_uses_explicit_app_data_dir_after_apply(tmp_path: Path):
    """`Settings.data_dir` honours APP_DATA_DIR for storage/work resolution."""
    app_data = tmp_path / "app"
    previous_data = os.environ.get("APP_DATA_DIR")
    previous_storage = os.environ.pop("STORAGE_LOCAL_ROOT", None)
    os.environ["APP_DATA_DIR"] = str(app_data)
    try:
        import config

        fresh = config.Settings()
        assert fresh.data_dir == app_data
        assert fresh.storage_dir == app_data / "storage"
    finally:
        if previous_data is None:
            os.environ.pop("APP_DATA_DIR", None)
        else:
            os.environ["APP_DATA_DIR"] = previous_data
        if previous_storage is not None:
            os.environ["STORAGE_LOCAL_ROOT"] = previous_storage


def _stub_entry(monkeypatch, events: list):
    """Replace runtime setup, readiness and the server run with recorders."""
    import types

    import desktop_entry
    import uvicorn

    monkeypatch.setenv("CONVERTER_INTERNAL_TOKEN", "t" * 40)
    monkeypatch.setenv("DESKTOP_BOOTSTRAP_SECRET", "t" * 40)
    monkeypatch.setenv("JWT_SECRET", "j" * 96)
    # main() flips this on for the real process; restored after the test.
    monkeypatch.setenv("DESKTOP_HOST_ENABLED", "false")
    monkeypatch.setattr(
        desktop_entry,
        "prepare_runtime",
        lambda: {"boot_id": "boot-order", "version": "9.9.9", "web_dist_available": False},
    )
    monkeypatch.setattr(
        desktop_entry,
        "publish_readiness",
        lambda port, boot_id, version, target: events.append(("ready", port)),
    )
    monkeypatch.setattr(
        uvicorn.Server,
        "run",
        lambda self, sockets=None: events.append(("run", sockets[0].getsockname()[1])),
    )
    return types, desktop_entry


def test_readiness_is_published_after_the_app_imports_and_before_serving(monkeypatch):
    """Readiness must follow `api.main` import and precede the server run."""
    import sys

    events: list = []
    types, desktop_entry = _stub_entry(monkeypatch, events)
    monkeypatch.setitem(sys.modules, "api.main", types.SimpleNamespace(app=object()))

    assert desktop_entry.main(["--port", "0"]) == 0
    assert [name for name, _ in events] == ["ready", "run"]
    ready_port = events[0][1]
    assert ready_port == events[1][1] > 0  # the port announced is the one bound


def test_broken_app_import_never_reports_ready(monkeypatch):
    """A failing `api.main` import (e.g. a missing DLL) must not announce READY."""
    import sys

    events: list = []
    _types, desktop_entry = _stub_entry(monkeypatch, events)
    # `None` in sys.modules makes `from api.main import app` raise ImportError.
    monkeypatch.setitem(sys.modules, "api.main", None)

    with pytest.raises(ImportError):
        desktop_entry.main(["--port", "0"])
    assert events == []
