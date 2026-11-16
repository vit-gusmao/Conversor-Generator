# Conversion engine image.
# Build context: repository root
#   docker build -f services/converter/Dockerfile -t pdf2x-converter .
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LOG_JSON=1

# Tesseract is the default OCR engine; OpenCV runtime libs for preprocessing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-por \
        libgl1 \
        libglib2.0-0 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/converter/pyproject.toml ./pyproject.toml
COPY services/converter/ ./

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -e ".[full,postgres]" \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /data/storage \
    && chown -R app:app /app /data

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/internal/v1/health || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
