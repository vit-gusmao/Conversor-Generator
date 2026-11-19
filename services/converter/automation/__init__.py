"""Automation (schedules/occurrences) domain (upgrade plan, docs 02/05).

Calendar rules, occurrence claims and the executor live here. Occurrences are
submissions over the existing durable conversion queue — this domain never
replaces it.
"""
