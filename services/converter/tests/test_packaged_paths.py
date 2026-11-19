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
