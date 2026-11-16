"""Job queue abstraction.

`inline` uses a bounded thread pool inside the API process (zero setup, used in
standalone mode); `celery` uses Redis for real horizontal scaling.  Both honour
cancellation and expose a health string for /health.
"""

from __future__ import annotations

import abc


class JobQueue(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def submit(self, run_id: str, payload: dict) -> None: ...

    @abc.abstractmethod
    def cancel(self, run_id: str) -> bool: ...

    @abc.abstractmethod
    def is_cancelled(self, run_id: str) -> bool: ...

    def health(self) -> str:
        return "up"

    def shutdown(self) -> None:
        return None


__all__ = ["JobQueue"]
