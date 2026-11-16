"""Visibility of the text layer: is the text actually drawn?

A PDF can carry text that is **invisible**: render mode ``3`` (invisible) or
``7`` (clip-only) makes the glyphs contribute nothing to the page a human sees.
Two corpus families depend on this:

* the whole visible page is an image and the text layer is an invisible OCR
  layer that may be **wrong** (it must lose to what the image shows);
* the page has *only* invisible text (nothing to look at): the hidden layer is
  the only source of information and must be exported, transparently flagged.

pdfminer (used by pdfplumber) does not expose the text render mode on
characters, so the page content stream is tokenised here.  Only the operators
that matter are interpreted; everything else is skipped, and malformed streams
degrade to "unknown" instead of raising.
"""

from __future__ import annotations

import io

_WHITESPACE = b" \t\r\n\f\x00"
_DELIMITERS = b"()<>[]{}/%"
_INVISIBLE_MODES = {3, 7}


def _read_literal(data: bytes, start: int) -> tuple[bytes, int]:
    """Read a ``(...)`` string, honouring escapes and nested parentheses."""
    out = bytearray()
    depth = 1
    index = start + 1
    length = len(data)
    while index < length:
        char = data[index]
        if char == 0x5C:  # backslash
            index += 1
            if index >= length:
                break
            escape = data[index]
            mapping = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}
            out.append(mapping.get(escape, escape))
            index += 1
            continue
        if char == 0x28:  # (
            depth += 1
            out.append(char)
            index += 1
            continue
        if char == 0x29:  # )
            depth -= 1
            index += 1
            if depth == 0:
                return bytes(out), index
            out.append(char)
            continue
        out.append(char)
        index += 1
    return bytes(out), length


def _decode_hex(data: bytes) -> bytes:
    digits = bytes(ch for ch in data if ch not in _WHITESPACE)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii", "ignore"))
    except ValueError:
        return b""


def _skip_dictionary(data: bytes, start: int) -> int:
    depth = 0
    index = start
    length = len(data)
    while index < length:
        pair = data[index : index + 2]
        if pair == b"<<":
            depth += 1
            index += 2
            continue
        if pair == b">>":
            depth -= 1
            index += 2
            if depth == 0:
                return index
            continue
        index += 1
    return length


def _skip_array(data: bytes, start: int) -> tuple[bytes, int]:
    """Collect the strings inside ``[ ... ]`` (a TJ array) and skip the rest."""
    strings = bytearray()
    index = start + 1
    length = len(data)
    while index < length:
        char = data[index : index + 1]
        if char == b"]":
            return bytes(strings), index + 1
        if char in (b"(",):
            text, index = _read_literal(data, index)
            strings += text
            continue
        if char == b"<":
            if data[index : index + 2] == b"<<":
                index = _skip_dictionary(data, index)
                continue
            end = data.find(b">", index)
            if end < 0:
                return bytes(strings), length
            strings += _decode_hex(data[index + 1 : end])
            index = end + 1
            continue
        if char == b"/":
            index += 1
            while index < length and data[index : index + 1] not in _WHITESPACE + _DELIMITERS:
                index += 1
            continue
        index += 1
    return bytes(strings), length


def invisible_ratio(stream: bytes) -> float | None:
    """Fraction of shown characters drawn with an invisible render mode.

    ``None`` means "no text operators found" — the caller must not infer
    anything from it.
    """
    mode = 0
    saved: list[int] = []
    operands: list[tuple[str, bytes]] = []
    total = 0
    invisible = 0
    index = 0
    length = len(stream)

    while index < length:
        char = stream[index : index + 1]
        if char in (b" ", b"\t", b"\r", b"\n", b"\f", b"\x00"):
            index += 1
            continue
        if char == b"%":
            end = stream.find(b"\n", index)
            index = length if end < 0 else end + 1
            continue
        if char == b"(":
            text, index = _read_literal(stream, index)
            operands.append(("string", text))
            continue
        if char == b"<":
            if stream[index : index + 2] == b"<<":
                index = _skip_dictionary(stream, index)
                operands.append(("other", b""))
                continue
            end = stream.find(b">", index)
            if end < 0:
                break
            operands.append(("string", _decode_hex(stream[index + 1 : end])))
            index = end + 1
            continue
        if char == b"[":
            strings, index = _skip_array(stream, index)
            operands.append(("string", strings))
            continue
        if char == b"]":
            index += 1
            continue
        if char == b"/":
            index += 1
            while index < length and stream[index : index + 1] not in _WHITESPACE + _DELIMITERS:
                index += 1
            operands.append(("name", b""))
            continue
        if char in (b"+", b"-", b".") or char.isdigit():
            start = index
            index += 1
            while index < length and (stream[index : index + 1] in (b"+", b"-", b".") or stream[index : index + 1].isdigit()):
                index += 1
            operands.append(("number", stream[start:index]))
            continue
        if char in (b"{", b"}"):
            index += 1
            continue
        # Operator token
        start = index
        while index < length and stream[index : index + 1] not in _WHITESPACE + _DELIMITERS:
            index += 1
        operator = stream[start:index]
        if not operator:
            index += 1
            continue

        if operator == b"q":
            saved.append(mode)
        elif operator == b"Q":
            mode = saved.pop() if saved else 0
        elif operator == b"Tr" and operands and operands[-1][0] == "number":
            try:
                mode = int(float(operands[-1][1].decode("ascii", "ignore")))
            except ValueError:
                pass
        elif operator in (b"Tj", b"TJ", b"'", b'"'):
            shown = sum(len(value) for kind, value in operands if kind == "string")
            if shown:
                total += shown
                if mode in _INVISIBLE_MODES:
                    invisible += shown
        # Every operand belongs to the operator just read; text strings were
        # already accounted for by 'shown'.
        operands.clear()

    if total == 0:
        return None
    return invisible / total


def page_invisible_ratios(data: bytes) -> list[float | None]:
    """One ratio per page of the document; ``[]`` when the PDF cannot be read."""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - dependency is declared
        return []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return []
        pages = []
        for page in reader.pages:
            try:
                stream = page.get_contents()
                raw = stream.get_data() if stream is not None else b""
            except Exception:  # noqa: BLE001
                raw = b""
            pages.append(invisible_ratio(raw))
        return pages
    except Exception:  # noqa: BLE001
        return []


__all__ = ["invisible_ratio", "page_invisible_ratios"]
