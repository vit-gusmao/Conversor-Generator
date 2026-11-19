"""Shared serialization helpers for the public API.

Kept free of other `api` imports so any layer can use them without cycles.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from datetime import UTC, datetime

_UNSAFE_HEADER_CHARS = re.compile(r'[\x00-\x1f\x7f"\\;]')


def iso_utc(value: datetime | None) -> str | None:
    """ISO-8601 timestamp with an explicit UTC offset.

    The database hands back naive datetimes for timestamps that were written in
    UTC, so a naive value is interpreted as UTC. Aware values are normalised to
    UTC. The output always ends in ``+00:00``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat()


def content_disposition(filename: str | None, disposition: str = "attachment") -> str:
    """``Content-Disposition`` header value safe for any Unicode filename.

    Starlette encodes header values as latin-1, so a raw non-latin-1 name fails
    the whole response. The header therefore carries an ASCII fallback in
    ``filename`` and the exact UTF-8 name in ``filename*`` (RFC 6266 / 5987).
    """
    raw = filename or "download"
    ascii_name = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    ascii_name = _UNSAFE_HEADER_CHARS.sub("_", ascii_name).strip() or "download"
    encoded = urllib.parse.quote(raw, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


__all__ = ["iso_utc", "content_disposition"]
