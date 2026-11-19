"""Automation (schedules/occurrences) domain (upgrade plan, docs 02/05).

Calendar rules, occurrence claims and the executor live here. Occurrences are
submissions over the existing durable conversion queue — this domain never
replaces it.
"""

# models.db re-exports these models at its bottom; importing it first keeps the
# package import order acyclic (AU1-01).
import models.db  # noqa: F401,E402
