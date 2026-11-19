"""Source registration and import domain (upgrade plan, docs 02/04).

The Python engine is the single writer of sources, imports and their
snapshots in both runtime modes. These modules hold the domain types and
(multiplexed in later tasks) the repository, path policy, scanner, snapshot
capture, importer and submission adapters.
"""

# models.db re-exports these models at its bottom; importing it first keeps the
# package import order acyclic (AU1-01).
import models.db  # noqa: F401,E402
