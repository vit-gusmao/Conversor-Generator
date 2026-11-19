"""Frozen-app entry point (T11, doc 03 §4).

Responsibilities, in order (each before the next):
1. read the environment the supervisor passes (absolute directories, port 0);
2. create mutable directories and refuse write locations inside resources;
3. run migrations/`init_db` with an exclusive lock and a consistent backup;
4. bind an OS-assigned port on loopback and publish an authenticated
   readiness payload (port, version, boot id) through a private file/stdout
   channel the Electron main process reads.

The process never assumes port 8000 is free — it binds first and reports the
port it actually got.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

ENGINE_VERSION_FALLBACK = "0.0.0"

DEV_INTERNAL_TOKEN = "dev-internal-token-change-me"
DEV_JWT_SECRET = "dev-jwt-secret-change-me"
MIN_SECRET_LENGTH = 16
#: Exit status for "refused to start: configuration" (EX_CONFIG).
EXIT_REFUSED = 78


class DesktopSecretError(RuntimeError):
    """The supervisor did not supply a usable secret; the process must not serve."""


def required_desktop_secrets(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Secrets the Electron supervisor must pass in the environment.

    `config.settings` reads the environment once, when it is first imported.
    A secret generated after that import is silently ignored and the documented
    development default stays active (AU1-23). So the process refuses to start
    unless every secret is supplied, and it never generates a fallback here.
    """
    env = os.environ if environ is None else environ
    rules: tuple[tuple[str, str | None], ...] = (
        ("CONVERTER_INTERNAL_TOKEN", DEV_INTERNAL_TOKEN),
        ("DESKTOP_BOOTSTRAP_SECRET", None),
        ("JWT_SECRET", DEV_JWT_SECRET),
    )
    values: dict[str, str] = {}
    for name, insecure in rules:
        value = (env.get(name) or "").strip()
        if len(value) < MIN_SECRET_LENGTH or value == insecure:
            raise DesktopSecretError(
                f"{name} não foi fornecido pelo aplicativo desktop "
                "(ausente, curto demais ou com o valor de desenvolvimento)."
            )
        values[name] = value
    return values


def verify_settings_secrets(settings_obj: object, supplied: Mapping[str, str]) -> None:
    """The settings singleton must carry exactly the supplied values."""
    actual = {
        "CONVERTER_INTERNAL_TOKEN": getattr(settings_obj, "converter_internal_token", None),
        "DESKTOP_BOOTSTRAP_SECRET": getattr(settings_obj, "desktop_bootstrap_secret", None),
        "JWT_SECRET": getattr(settings_obj, "jwt_secret", None),
    }
    for name, value in actual.items():
        if value != supplied[name]:
            raise DesktopSecretError(f"a configuração {name} não reflete o valor fornecido.")


def _boot_id() -> str:
    return uuid.uuid4().hex


def _free_loopback_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a free port; the caller binds it immediately after."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def prepare_runtime() -> dict:
    """Configure paths, run schema setup and return readiness metadata."""
    from runtime.paths import RuntimePaths

    paths = RuntimePaths.from_environment()
    paths.assert_isolated_from_resources()
    paths.ensure()
    paths.apply()

    # Exclusive lock, refusal of a newer schema and a consistent backup happen
    # inside this call, before any table is created or altered (T11).
    from models.db import get_engine
    from runtime.db_migrate import run_startup_migrations

    run_startup_migrations(paths.app_data_dir, get_engine())

    # The singleton has just been created from the environment: prove it holds
    # the supplied secrets, not the development defaults.
    from config import settings

    verify_settings_secrets(settings, required_desktop_secrets())

    try:
        from version import ENGINE_VERSION  # type: ignore
    except Exception:  # noqa: BLE001
        ENGINE_VERSION = ENGINE_VERSION_FALLBACK

    return {
        "boot_id": _boot_id(),
        "version": ENGINE_VERSION,
        "app_data_dir": str(paths.app_data_dir),
        "work_dir": str(paths.work_dir),
        "web_dist_available": paths.web_dist_available,
    }


def publish_readiness(port: int, boot_id: str, version: str, target: Path | None) -> None:
    payload = json.dumps({"port": port, "boot_id": boot_id, "version": version, "pid": os.getpid()})
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(".tmp")
        staging.write_text(payload, encoding="utf-8")
        os.replace(staging, target)
    # stdout is the private pipe the supervisor reads in development.
    print(f"CONVERTER_READY {payload}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conversor Generator backend (frozen entry)")
    parser.add_argument("--host", default=os.environ.get("CONVERTER_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CONVERTER_PORT", "0")),
        help="0 asks the OS for a free port (desktop default)",
    )
    parser.add_argument(
        "--readiness-file",
        default=os.environ.get("CONVERTER_READINESS_FILE"),
        help="Where to publish {port, boot_id, version} for the supervisor",
    )
    parser.add_argument("--no-serve", action="store_true", help="Prepare runtime and exit (tests)")
    args = parser.parse_args(argv)

    # Refuse before ANY import that creates config.settings (AU1-23).
    try:
        required_desktop_secrets()
    except DesktopSecretError as exc:
        print(f"CONVERTER_REFUSED {exc}", file=sys.stderr, flush=True)
        return EXIT_REFUSED
    # Desktop mode is always host-guarded; the supervisor's profile sets this too.
    os.environ["DESKTOP_HOST_ENABLED"] = "true"

    from runtime.db_migrate import MigrationLockTimeout, SchemaTooNewError

    try:
        metadata = prepare_runtime()
    except (SchemaTooNewError, MigrationLockTimeout) as exc:
        # The database was not modified: refuse to serve it.
        print(f"CONVERTER_REFUSED {exc}", file=sys.stderr, flush=True)
        return EXIT_REFUSED
    if args.no_serve:
        # Same guarantee as serving: READY only after the ASGI app imports, so
        # the smoke test catches a broken bundle (e.g. a missing DLL).
        from api.main import app  # noqa: F401

        publish_readiness(0, metadata["boot_id"], metadata["version"], None)
        return 0

    # Bind the listening socket FIRST and hand it to uvicorn: readiness must
    # announce the port that is actually accepting connections (no probe-then-
    # assume race, and the window can load immediately).
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(2048)
    port = int(listener.getsockname()[1])

    # Readiness is published only after the ASGI app imports cleanly: a broken
    # bundle (missing DLL, bad module) must fail here, never look "ready".
    try:
        import uvicorn

        from api.main import app
    except BaseException:
        try:
            listener.close()
        except OSError:
            pass
        raise

    publish_readiness(
        port,
        metadata["boot_id"],
        metadata["version"],
        Path(args.readiness_file) if args.readiness_file else None,
    )

    config = uvicorn.Config(
        app, log_level=os.environ.get("LOG_LEVEL", "info").lower(), lifespan="on"
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[listener])
    finally:
        try:
            listener.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
