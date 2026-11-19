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
import secrets
import socket
import sys
import uuid
from pathlib import Path

ENGINE_VERSION_FALLBACK = "0.0.0"


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

    from models.db import init_db

    init_db()

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

    metadata = prepare_runtime()
    if args.no_serve:
        publish_readiness(0, metadata["boot_id"], metadata["version"], None)
        return 0

    port = args.port or _free_loopback_port(args.host)

    # The bootstrap secret authenticates privileged requests from the host.
    if not os.environ.get("DESKTOP_BOOTSTRAP_SECRET"):
        os.environ["DESKTOP_BOOTSTRAP_SECRET"] = secrets.token_urlsafe(32)

    publish_readiness(
        port,
        metadata["boot_id"],
        metadata["version"],
        Path(args.readiness_file) if args.readiness_file else None,
    )

    import uvicorn

    from api.main import app

    uvicorn.run(app, host=args.host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
