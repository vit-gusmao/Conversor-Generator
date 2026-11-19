"""Full-mode automation runner (T8, doc 05 §9).

The full deployment keeps the HTTP API with the executor disabled and runs
this module as its own service against the engine database:

    python -m automation.cli --poll-seconds 5

It shares the same calendar, claims and executor as the standalone loop; only
the submission adapter differs (gateway HTTP instead of local services).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from automation.loop import AutomationLoop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conversor Generator automation runner")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(__import__("os").environ.get("AUTOMATION_POLL_SECONDS", "5")),
    )
    parser.add_argument("--worker-id", default="full-automation")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logger = logging.getLogger("automation.cli")

    loop = AutomationLoop(poll_seconds=args.poll_seconds, worker_id=args.worker_id)
    if args.once:
        summary = loop.tick()
        logger.info("tick: %s", summary)
        return 0

    stop = threading.Event()

    def _handle(signum, _frame):  # noqa: ANN001
        logger.info("signal %s: encerrando", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    loop.start()
    logger.info("automation runner ativo (poll=%ss)", args.poll_seconds)
    try:
        stop.wait()
    finally:
        loop.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
