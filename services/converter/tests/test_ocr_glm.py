"""Unit tests for the GLM-OCR engine adapter (engine/ocr/glmocr.py).

The HTTP layer is exercised through injected fakes — no real network in the
unit suite.  One integration test talks to the real local Ollama service and
skips automatically when it is not available.
"""

from __future__ import annotations

import base64

import pytest

from config import settings
from engine.ocr import glmocr
from engine.ocr.base import OcrUnavailable
from engine.ocr.engines import GlmOcrEngine, NullEngine, get_engine
from engine.ocr.glmocr import (
    TASK_TABLE_PROMPT,
    TASK_TEXT_PROMPT,
    GlmOcrClient,
    GlmOcrError,
    even_line_boxes,
    grid_boxes,
    looks_like_html_table,
    parse_html_table,
    probe_service,
    segment_text_lines,
    strip_html,
    words_in_box,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self) -> dict:
        return self._body


def _fake_poster(response_text: str, status_code: int = 200):
    calls: list[dict] = []

    def poster(url, json=None, timeout=None):  # noqa: ANN001, A002
        calls.append({"url": url, "payload": json})
        return _FakeResponse(status_code, {"response": response_text})

    return poster, calls


def _client(poster) -> GlmOcrClient:
    return GlmOcrClient(poster=poster, getter=lambda *a, **k: _FakeResponse(200, {"models": []}))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TestGlmOcrClient:
    def test_generate_success_returns_stripped_text(self):
        poster, calls = _fake_poster("  linha um\nlinha dois  \n")
        client = _client(poster)
        assert client.generate(TASK_TEXT_PROMPT, b"png-bytes") == "linha um\nlinha dois"
        payload = calls[0]["payload"]
        assert payload["model"] == settings.glmocr_model
        assert payload["prompt"] == TASK_TEXT_PROMPT
        assert payload["stream"] is False
        assert payload["keep_alive"] == settings.glmocr_keep_alive
        assert payload["options"]["temperature"] == 0
        assert payload["options"]["num_ctx"] == settings.glmocr_num_ctx
        assert payload["options"]["num_predict"] == settings.glmocr_num_predict
        assert payload["images"] == [base64.b64encode(b"png-bytes").decode("ascii")]

    def test_generate_strips_think_blocks(self):
        poster, _ = _fake_poster("<think>reasoning...</think>R$ 1.234,56")
        client = _client(poster)
        assert client.generate(TASK_TEXT_PROMPT, b"x") == "R$ 1.234,56"

    def test_generate_http_error_raises_glm_error(self):
        poster, _ = _fake_poster("", status_code=503)
        client = _client(poster)
        with pytest.raises(GlmOcrError, match="HTTP 503"):
            client.generate(TASK_TEXT_PROMPT, b"x")

    def test_generate_service_error_field_raises(self):
        poster = lambda *a, **k: _FakeResponse(200, {"error": "model not found"})  # noqa: E731
        client = _client(poster)
        with pytest.raises(GlmOcrError, match="model not found"):
            client.generate(TASK_TEXT_PROMPT, b"x")

    def test_generate_empty_output_raises(self):
        poster, _ = _fake_poster("   \n  ")
        client = _client(poster)
        with pytest.raises(GlmOcrError, match="empty"):
            client.generate(TASK_TEXT_PROMPT, b"x")

    def test_transport_failure_raises_unavailable(self):
        def poster(*a, **k):  # noqa: ANN002
            raise ConnectionError("connection refused")

        client = _client(poster)
        with pytest.raises(OcrUnavailable, match="unreachable"):
            client.generate(TASK_TEXT_PROMPT, b"x")

    def test_num_gpu_is_forwarded_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "glmocr_num_gpu", 99)
        poster, calls = _fake_poster("ok")
        client = _client(poster)
        client.generate(TASK_TEXT_PROMPT, b"x")
        assert calls[0]["payload"]["options"]["num_gpu"] == 99

    def test_model_present_matches_exact_and_prefix(self):
        body = {"models": [{"name": "glm-ocr:latest"}, {"name": "gemma4:12b"}]}
        getter = lambda *a, **k: _FakeResponse(200, body)  # noqa: E731
        assert GlmOcrClient(getter=getter).model_present() is True
        assert GlmOcrClient(getter=getter, model="glm-ocr").model_present() is True

    def test_model_present_false_when_absent_or_error(self):
        body = {"models": [{"name": "gemma4:12b"}]}
        getter = lambda *a, **k: _FakeResponse(200, body)  # noqa: E731
        assert GlmOcrClient(getter=getter).model_present() is False
        boom = lambda *a, **k: (_ for _ in ()).throw(ConnectionError())  # noqa: E731
        assert GlmOcrClient(getter=boom).model_present() is False


# ---------------------------------------------------------------------------
# Deterministic geometry
# ---------------------------------------------------------------------------

def _bars_image(bars: list[tuple[int, int]], width: int = 300):
    from PIL import Image
    import numpy as np

    arr = np.full((140, width), 255, dtype="uint8")
    for y0, y1 in bars:
        arr[y0:y1, 20:260] = 0
    return Image.fromarray(arr, mode="L").convert("RGB")


class TestSegmentTextLines:
    def test_two_bars_produce_two_ordered_boxes(self):
        boxes = segment_text_lines(_bars_image([(10, 30), (70, 90)]))
        assert len(boxes) == 2
        assert [round(b[1]) for b in boxes] == [10, 70]
        # x extent follows the real ink columns (20..260)
        assert boxes[0][0] == 20.0
        assert boxes[0][2] == 260.0

    def test_blank_image_has_no_lines(self):
        assert segment_text_lines(_bars_image([])) == []

    def test_accent_gap_is_merged(self):
        # An accent mark sits a few pixels above the letter body of the same
        # line: a small gap relative to the line height must merge.
        boxes = segment_text_lines(_bars_image([(20, 50), (53, 71)]))
        assert len(boxes) == 1
        assert boxes[0][1] == 20.0
        assert boxes[0][3] == 71.0


class TestWordsInBox:
    def test_words_are_proportional_and_ordered(self):
        words = words_in_box("AB CD", (10.0, 5.0, 110.0, 25.0), confidence=0.9)
        assert [w.text for w in words] == ["AB", "CD"]
        assert words[0].bbox_px[0] < words[1].bbox_px[0]
        assert all(w.confidence == 0.9 for w in words)
        assert all(w.bbox_px[3] == 25.0 for w in words)
        # stays inside the ink extent
        assert words[-1].bbox_px[2] <= 110.0 + 1e-6

    def test_empty_text_returns_no_words(self):
        assert words_in_box("   ", (0, 0, 10, 10), 0.9) == []


class TestEvenLineBoxes:
    def test_distribution_covers_height(self):
        boxes = even_line_boxes(200, 100, 4)
        assert len(boxes) == 4
        assert boxes[0][1] == 0.0
        assert boxes[-1][3] == 200.0
        assert [round(b[3] - b[1], 3) for b in boxes] == [50.0] * 4

    def test_zero_count(self):
        assert even_line_boxes(100, 100, 0) == []


class TestHtmlTableParsing:
    HTML = (
        "<table border=\"1\"><tr><td>Produto</td><td>Qtd</td><td>Valor</td></tr>"
        "<tr><td>Notebook</td><td>2</td><td>R$ 5.000,00</td></tr>"
        "<tr><td>Mouse</td><td>10</td><td>R$ 250,00</td></tr></table>"
    )

    def test_parse_rows_and_cells(self):
        rows = parse_html_table(self.HTML)
        assert len(rows) == 3
        assert rows[0] == ["Produto", "Qtd", "Valor"]
        assert rows[2][2] == "R$ 250,00"

    def test_non_table_input_returns_empty(self):
        assert parse_html_table("apenas texto") == []

    def test_strip_html_removes_markup(self):
        assert strip_html("<b>Total</b>: 10") == "Total: 10"
        assert strip_html("<td>a</td><td>b</td>") == "a b"

    def test_looks_like_html_table(self):
        assert looks_like_html_table(self.HTML) is True
        assert looks_like_html_table("Nome: X") is False

    def test_grid_boxes_shape(self):
        boxes = grid_boxes(300, 200, 2, 3)
        assert len(boxes) == 6
        assert boxes[0] == (0.0, 0.0, 100.0, 100.0)
        assert boxes[-1] == (200.0, 100.0, 300.0, 200.0)


class TestTableRecognitionOutput:
    def test_table_mode_never_emits_raw_html(self):
        poster, calls = _fake_poster(TestHtmlTableParsing.HTML)
        engine = GlmOcrEngine(client=_client(poster))
        words = engine.recognize(_bars_image([(60, 80)]), mode="table")
        assert engine.boxing_method == "html_grid_v1"
        texts = [w.text for w in words]
        assert "Notebook" in texts
        assert "<td>" not in " ".join(texts)
        assert "<table" not in " ".join(texts)

        # Rebuild each row from the token geometry: reading order and cell
        # layout must survive the conversion from HTML to tokens.
        rows: dict[float, list] = {}
        for word in words:
            rows.setdefault(round(word.bbox_px[1], 3), []).append(word)
        rebuilt = [
            " ".join(w.text for w in sorted(row, key=lambda w: w.bbox_px[0]))
            for _, row in sorted(rows.items())
        ]
        assert rebuilt == [
            "Produto Qtd Valor",
            "Notebook 2 R$ 5.000,00",
            "Mouse 10 R$ 250,00",
        ]

    def test_table_html_detected_even_in_page_mode(self):
        poster, _ = _fake_poster(TestHtmlTableParsing.HTML)
        engine = GlmOcrEngine(client=_client(poster))
        words = engine.recognize(_bars_image([(60, 80)]), mode="page")
        assert engine.boxing_method == "html_grid_v1"
        assert "<td>" not in " ".join(w.text for w in words)

    def test_grid_cells_are_positioned_by_row_and_column(self):
        poster, _ = _fake_poster("<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>")
        engine = GlmOcrEngine(client=_client(poster))
        words = engine.recognize(_bars_image([(60, 80)]), mode="table")
        by_text = {w.text: w for w in words}
        assert by_text["A"].bbox_px[1] < by_text["C"].bbox_px[1]  # row order
        assert by_text["A"].bbox_px[0] < by_text["B"].bbox_px[0]  # column order



# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TestGlmOcrEngine:
    def test_recognize_projection_path_1to1(self):
        poster, _ = _fake_poster("Nome: Joao\nValor: 1234")
        engine = GlmOcrEngine(client=_client(poster))
        image = _bars_image([(10, 30), (70, 90)])
        words = engine.recognize(image, mode="page")
        assert engine.boxing_method == "projection_v1"
        bands = sorted({round(w.bbox_px[1]) for w in words})
        assert bands == [10, 70]
        assert [w.text for w in words if w.text.startswith("Nome")] == ["Nome:"]
        assert all(w.confidence == settings.glmocr_confidence for w in words)

    def test_recognize_falls_back_to_even_distribution(self):
        poster, _ = _fake_poster("um\ndois\ntres")
        engine = GlmOcrEngine(client=_client(poster))
        words = engine.recognize(_bars_image([(60, 80)]), mode="page")
        assert engine.boxing_method == "even_distribution"
        bands = sorted({round(w.bbox_px[1]) for w in words})
        assert len(bands) == 3

    def test_recognize_table_prompt_for_table_mode(self):
        poster, calls = _fake_poster("<table><tr><td>a</td></tr></table>")
        engine = GlmOcrEngine(client=_client(poster))
        words = engine.recognize(_bars_image([(60, 80)]), mode="table")
        assert calls[0]["payload"]["prompt"] == TASK_TABLE_PROMPT
        assert [w.text for w in words] == ["a"]

    def test_recognize_failure_raises_unavailable(self):
        def poster(*a, **k):  # noqa: ANN002
            return _FakeResponse(200, {"error": "oom"})

        engine = GlmOcrEngine(client=_client(poster))
        with pytest.raises(OcrUnavailable):
            engine.recognize(_bars_image([(60, 80)]), mode="page")

    def test_describe_exposes_audit_metadata(self):
        engine = GlmOcrEngine(client=_client(_fake_poster("x")[0]))
        details = engine.describe()
        assert details["engine"] == "glm-ocr"
        assert details["model"] == settings.glmocr_model
        assert "/api/generate" in str(details["api"])
        assert details["confidence_policy"] == "constant_vlm"


class TestAvailabilityAndRegistry:
    def test_available_follows_probe(self, monkeypatch):
        monkeypatch.setattr(settings, "glmocr_enabled", True)
        monkeypatch.setattr(glmocr, "probe_service", lambda *a, **k: True)
        assert GlmOcrEngine.available() is True
        monkeypatch.setattr(glmocr, "probe_service", lambda *a, **k: False)
        assert GlmOcrEngine.available() is False

    def test_available_disabled_by_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "glmocr_enabled", False)
        assert GlmOcrEngine.available() is False

    def test_probe_service_caches_result(self, monkeypatch):
        glmocr._probe_cache.clear()
        calls = {"tcp": 0, "tags": 0}

        def fake_tcp(host, port, timeout=1.0):
            calls["tcp"] += 1
            return True

        class FakeClient:
            def model_present(self):
                calls["tags"] += 1
                return True

        monkeypatch.setattr(glmocr, "_tcp_reachable", fake_tcp)
        monkeypatch.setattr(glmocr, "GlmOcrClient", FakeClient)
        monkeypatch.setattr(settings, "glmocr_probe_cache_seconds", 600)
        assert probe_service(settings.glmocr_host, settings.glmocr_port, settings.glmocr_model) is True
        assert probe_service(settings.glmocr_host, settings.glmocr_port, settings.glmocr_model) is True
        assert calls == {"tcp": 1, "tags": 1}
        glmocr._probe_cache.clear()

    def test_registry_prefers_glm_when_available(self, monkeypatch):
        monkeypatch.setenv("CONVERTER_OCR_ENGINE", "glm-ocr")
        monkeypatch.setattr(glmocr, "probe_service", lambda *a, **k: True)
        assert get_engine().name == "glm-ocr"

    def test_registry_falls_back_when_glm_down(self, monkeypatch):
        monkeypatch.setenv("CONVERTER_OCR_ENGINE", "glm-ocr")
        monkeypatch.setattr(glmocr, "probe_service", lambda *a, **k: False)
        # no rapidocr/tesseract installed in this environment → NullEngine
        engine = get_engine()
        assert engine.name in {"none", "rapidocr", "tesseract"}
        assert isinstance(engine, NullEngine) or engine.name != "glm-ocr"


# ---------------------------------------------------------------------------
# Real integration (skipped when the local Ollama service is not reachable)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not GlmOcrEngine.available(),
    reason="Ollama com glm-ocr não disponível neste ambiente",
)
class TestRealGlmOcrIntegration:
    def test_recognize_real_service(self):
        glmocr._probe_cache.clear()
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (700, 220), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("arial.ttf", 26)
        except OSError:
            font = ImageFont.load_default()
        draw.text((30, 30), "Nome: Maria Souza", fill="black", font=font)
        draw.text((30, 90), "Total: R$ 9.876,54", fill="black", font=font)
        draw.text((30, 150), "Data: 15/11/2026", fill="black", font=font)

        engine = GlmOcrEngine()
        words = engine.recognize(image, mode="page")
        text = " ".join(w.text for w in words)
        assert "Maria" in text
        assert "9.876,54" in text
        assert all(w.bbox_px[1] >= 0 for w in words)
        assert engine.boxing_method in {"projection_v1", "even_distribution"}
