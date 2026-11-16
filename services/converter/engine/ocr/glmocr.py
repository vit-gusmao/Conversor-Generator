"""GLM-OCR engine — VLM recognition via local Ollama (``glm-ocr:latest``).

GLM-OCR integration: the model
runs 100% locally on the GPU through Ollama's native ``/api/generate`` endpoint
(the endpoint recommended by the official repository for GLM-OCR), with
``temperature=0`` and explicit ``num_ctx`` to avoid truncated pages.

The VLM returns the *content* of the image (one text line per row, or an HTML
``<table>`` for ``Table Recognition:``) but **no word coordinates**.  The rest
of this engine (blocks → lines → fields → tables) is built on real word
geometry, so this module reconstructs geometry deterministically and records
exactly how:

* a horizontal ink-projection (NumPy) segments the processed raster into text
  lines; GLM-OCR output lines are matched 1:1 when the counts agree
  (``boxing="projection_v1"``);
* when the counts disagree the output lines are distributed evenly over the
  image height (``boxing="even_distribution"``) — never silently dropped;
* word x-positions inside a line are proportional to character counts, clamped
  to the line's real ink extent.

Nothing here invents content: an empty model answer raises ``OcrUnavailable``
and the page stays explicitly unrecovered in the audit.
"""

from __future__ import annotations

import base64
import io
import re
import socket
import time
from typing import Callable

from config import settings
from engine.ocr.base import OcrEngine, OcrUnavailable, OcrWord

# Task prompts documented by the GLM-OCR repository for each content class.
TASK_TEXT_PROMPT = "Text Recognition:"
TASK_TABLE_PROMPT = "Table Recognition:"
TASK_FORMULA_PROMPT = "Formula Recognition:"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)


class GlmOcrError(RuntimeError):
    """The GLM-OCR service answered, but the answer is unusable."""


def _strip_think(text: str) -> str:
    """Remove optional reasoning blocks some GLM checkpoints emit."""
    return _THINK_RE.sub("", text or "").strip()


_BLOCK_TAG_RE = re.compile(
    r"</?(?:tr|td|th|table|thead|tbody|p|div|br|li|ul|ol|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    """Flatten markup to plain text (raw HTML is never exported).

    Block-level tags become a space so neighbouring cells do not fuse into a
    single word; inline tags disappear without leaving gaps.
    """
    if not text:
        return ""
    spaced = _BLOCK_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", _TAG_RE.sub("", spaced)).strip()


def looks_like_html_table(text: str) -> bool:
    return bool(_HTML_TABLE_RE.search(text or ""))


def parse_html_table(html: str) -> list[list[str]]:
    """Parse GLM-OCR ``Table Recognition:`` output into a list of rows.

    Returns cell texts with markup removed, ``None`` for unparsable output.
    Empty cells are preserved so column alignment survives.
    """
    if not looks_like_html_table(html):
        return []
    rows: list[list[str]] = []
    for row_html in _ROW_RE.findall(html):
        cells = [strip_html(c) for c in _CELL_RE.findall(row_html)]
        if cells:
            rows.append(cells)
    return rows


class GlmOcrClient:
    """Minimal Ollama ``/api/generate`` client shaped for GLM-OCR.

    The request/response format mirrors the official ``glmocr`` SDK
    (``api_mode: ollama_generate``), verified against the local service; the
    SDK's full pipeline additionally needs the heavy torch layout extra, which
    is optional here — this converter performs its own deterministic geometry.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        request_timeout: int | None = None,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | None = None,
        num_gpu: int | None = None,
        poster: Callable[..., object] | None = None,
        getter: Callable[..., object] | None = None,
    ) -> None:
        self.base_url = (base_url or settings.glmocr_base_url).rstrip("/")
        self.model = model or settings.glmocr_model
        self.request_timeout = request_timeout or settings.glmocr_request_timeout
        self.num_ctx = num_ctx if num_ctx is not None else settings.glmocr_num_ctx
        self.num_predict = num_predict if num_predict is not None else settings.glmocr_num_predict
        self.keep_alive = keep_alive if keep_alive is not None else settings.glmocr_keep_alive
        # None keeps Ollama's automatic GPU offload (all layers when VRAM fits).
        self.num_gpu = num_gpu if num_gpu is not None else settings.glmocr_num_gpu
        self._poster = poster
        self._getter = getter

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, payload: dict) -> object:
        import httpx  # already a project dependency

        poster = self._poster or httpx.post
        return poster(
            f"{self.base_url}{path}",
            json=payload,
            timeout=httpx.Timeout(self.request_timeout, connect=5.0),
        )

    def generate(self, prompt: str, image_png: bytes) -> str:
        """Run one recognition task and return the model text output."""
        options: dict[str, object] = {
            "temperature": 0,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "images": [base64.b64encode(image_png).decode("ascii")],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        try:
            response = self._post("/api/generate", payload)
        except Exception as exc:  # noqa: BLE001 - transport failures are availability failures
            raise OcrUnavailable(f"glm-ocr service unreachable: {exc}") from exc

        try:
            status = int(getattr(response, "status_code", 0))
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise GlmOcrError(f"glm-ocr returned an invalid HTTP response: {exc}") from exc

        if status != 200:
            raise GlmOcrError(f"glm-ocr HTTP {status}")

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise GlmOcrError(f"glm-ocr service error: {error}")

        text = _strip_think(str(body.get("response", "")) if isinstance(body, dict) else "")
        if not text:
            raise GlmOcrError("glm-ocr returned an empty recognition output")
        return text

    def model_present(self) -> bool:
        """True when the configured model tag exists in the Ollama registry."""
        import httpx  # already a project dependency

        getter = self._getter or httpx.get
        try:
            response = getter(
                f"{self.base_url}/api/tags",
                timeout=httpx.Timeout(4.0, connect=2.0),
            )
            if int(getattr(response, "status_code", 0)) != 200:
                return False
            models = response.json().get("models", [])
        except Exception:  # noqa: BLE001
            return False
        wanted = self.model.lower()
        for item in models:
            name = str(item.get("name", "")).lower()
            if name == wanted or name.split(":")[0] == wanted.split(":")[0]:
                return True
        return False


# ---------------------------------------------------------------------------
# Deterministic geometry — ink projection over the processed raster
# ---------------------------------------------------------------------------

def segment_text_lines(image, *, threshold: int = 128) -> list[tuple[float, float, float, float]]:
    """Locate text lines as (x0, y0, x1, y1) pixel boxes via row projection.

    Merges ink runs separated by small vertical gaps (accents, thin strokes)
    and drops runs too small to be text.  Returns boxes ordered top→bottom.
    """
    import numpy as np

    gray = np.asarray(image.convert("L"))
    if gray.size == 0:
        return []
    ink = gray < threshold
    if not ink.any():
        return []

    row_ink = ink.sum(axis=1)
    rows = np.flatnonzero(row_ink > 0)
    runs: list[list[int]] = []
    start = prev = int(rows[0])
    for r in rows[1:]:
        r = int(r)
        if r - prev <= 1:
            prev = r
            continue
        runs.append([start, prev])
        start = prev = r
    runs.append([start, prev])

    heights = [b - a + 1 for a, b in runs]
    median_h = float(np.median(heights)) if heights else 0.0
    merge_gap = max(2, int(median_h * 0.45))

    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(list(run))
    # Drop noise specks that survived merging.
    merged = [m for m in merged if (m[1] - m[0] + 1) >= max(3, int(median_h * 0.25))]

    boxes: list[tuple[float, float, float, float]] = []
    for y0, y1 in merged:
        cols = ink[y0 : y1 + 1].sum(axis=0)
        xs = np.flatnonzero(cols > 0)
        if xs.size == 0:
            continue
        boxes.append((float(xs[0]), float(y0), float(xs[-1] + 1), float(y1 + 1)))
    return boxes


def words_in_box(text: str, box: tuple[float, float, float, float], confidence: float) -> list[OcrWord]:
    """Split one line of text into words laid out proportionally in *box*.

    x-positions are derived from character counts inside the real ink extent —
    the audit records this policy, so no fabricated precision is implied.
    """
    x0, y0, x1, y1 = box
    words = [w for w in text.split() if w]
    if not words:
        return []
    span = max(x1 - x0, 1.0)
    total_chars = sum(len(w) for w in words) + max(len(words) - 1, 0)
    char_w = span / max(total_chars, 1)
    cursor = x0
    result: list[OcrWord] = []
    for i, word in enumerate(words):
        width = max(char_w * len(word), 1.0)
        result.append(
            OcrWord(
                text=word,
                bbox_px=(cursor, y0, cursor + width, y1),
                confidence=confidence,
            )
        )
        cursor += width + (char_w if i < len(words) - 1 else 0.0)
    return result


def even_line_boxes(height: int, width: int, count: int) -> list[tuple[float, float, float, float]]:
    """Distribute *count* line boxes evenly over the page height."""
    return distribute_boxes(width, 0.0, float(height), count)


def distribute_boxes(
    width: int, top: float, bottom: float, count: int
) -> list[tuple[float, float, float, float]]:
    """Split the vertical span [top, bottom] into *count* equal line boxes."""
    if count <= 0:
        return []
    span = max(bottom - top, float(count))
    step = span / count
    margin = width * 0.02
    return [
        (margin, top + i * step, width - margin, top + (i + 1) * step)
        for i in range(count)
    ]


def grid_boxes(
    width: int, height: int, rows: int, cols: int
) -> list[tuple[float, float, float, float]]:
    """Cell boxes for an inferred rows x cols grid over the whole image."""
    if rows <= 0 or cols <= 0:
        return []
    cell_w = width / cols
    cell_h = height / rows
    return [
        (c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h)
        for r in range(rows)
        for c in range(cols)
    ]


# ---------------------------------------------------------------------------
# Service probe (cheap, cached)
# ---------------------------------------------------------------------------

_probe_cache: dict[str, tuple[float, bool]] = {}


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def probe_service(host: str, port: int, model: str, *, cache_seconds: float | None = None) -> bool:
    """True when Ollama answers and carries the configured model (cached)."""
    ttl = cache_seconds if cache_seconds is not None else settings.glmocr_probe_cache_seconds
    key = f"{host}:{port}:{model}"
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1]
    ok = _tcp_reachable(host, port) and GlmOcrClient().model_present()
    _probe_cache[key] = (now, ok)
    return ok


# ---------------------------------------------------------------------------
# Engine adapter
# ---------------------------------------------------------------------------

class GlmOcrEngine(OcrEngine):
    """Recognition engine backed by the local GLM-OCR VLM (Ollama, GPU)."""

    name = "glm-ocr"
    kind = "ocr"

    def __init__(self, client: GlmOcrClient | None = None) -> None:
        self._client = client or GlmOcrClient()
        self.version = self._client.model
        self.boxing_method = "projection_v1"

    @classmethod
    def available(cls) -> bool:
        if not settings.glmocr_enabled:
            return False
        try:
            return probe_service(settings.glmocr_host, settings.glmocr_port, settings.glmocr_model)
        except Exception:  # noqa: BLE001 - a broken probe must never crash selection
            return False

    @staticmethod
    def _prompt_for(mode: str) -> str:
        if mode in {"table", "table_html"}:
            return TASK_TABLE_PROMPT
        if mode == "formula":
            return TASK_FORMULA_PROMPT
        return TASK_TEXT_PROMPT

    def recognize(self, image, *, mode: str = "page", lang: str | None = None) -> list[OcrWord]:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        try:
            text = self._client.generate(self._prompt_for(mode), buffer.getvalue())
        except GlmOcrError as exc:
            raise OcrUnavailable(str(exc)) from exc

        width, height = image.size
        confidence = settings.glmocr_confidence

        # Table Recognition answers with an HTML table.  Raw markup must never
        # reach the workbook, so it is parsed into a grid: one token per cell,
        # geometry from the inferred rows x cols layout.
        if mode in {"table", "table_html"} or looks_like_html_table(text):
            rows = parse_html_table(text)
            if not rows:
                # Not a usable table: fall back to the plain-text path so the
                # content is still preserved (never emitted as raw HTML).
                text = strip_html(text)
            else:
                self.boxing_method = "html_grid_v1"
                cols = max(len(r) for r in rows)
                boxes = grid_boxes(width, height, len(rows), cols)
                words: list[OcrWord] = []
                for row_index, row in enumerate(rows):
                    for col_index in range(cols):
                        cell = row[col_index] if col_index < len(row) else ""
                        if not cell:
                            continue  # empty cell keeps its position implicitly
                        box = boxes[row_index * cols + col_index]
                        words.extend(words_in_box(cell, box, confidence))
                if words:
                    return words
                raise OcrUnavailable("glm-ocr returned a table without any cell content")

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise OcrUnavailable("glm-ocr returned no text lines")

        boxes = segment_text_lines(image)
        if len(boxes) == len(lines):
            self.boxing_method = "projection_v1"
        else:
            # Count mismatch: keep every line, anchored to the ink extent (not
            # the whole page) so text stays where the document actually has it.
            self.boxing_method = "even_distribution"
            if boxes:
                top = min(b[1] for b in boxes)
                bottom = max(b[3] for b in boxes)
                boxes = distribute_boxes(width, top, bottom, len(lines))
            else:
                boxes = even_line_boxes(height, width, len(lines))

        words = []
        for line, box in zip(lines, boxes):
            words.extend(words_in_box(line, box, confidence))
        return words

    def describe(self) -> dict[str, object]:
        """Audit metadata: how recognition and geometry were produced."""
        return {
            "engine": self.name,
            "model": self._client.model,
            "api": f"{self._client.base_url}/api/generate",
            "num_ctx": self._client.num_ctx,
            "keep_alive": self._client.keep_alive,
            "gpu": "auto" if self._client.num_gpu is None else self._client.num_gpu,
            "boxing_method": self.boxing_method,
            "confidence_policy": "constant_vlm",
            "confidence_value": settings.glmocr_confidence,
        }


__all__ = [
    "GlmOcrClient",
    "GlmOcrEngine",
    "GlmOcrError",
    "TASK_TEXT_PROMPT",
    "TASK_TABLE_PROMPT",
    "TASK_FORMULA_PROMPT",
    "segment_text_lines",
    "words_in_box",
    "even_line_boxes",
    "distribute_boxes",
    "grid_boxes",
    "parse_html_table",
    "strip_html",
    "looks_like_html_table",
    "probe_service",
]
