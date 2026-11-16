# Motor de conversão (Python)

Serviço FastAPI que executa o pipeline PDF → XLSX e persiste toda a
proveniência. Roda em dois modos:

| Modo | Comando | Quem chama |
| --- | --- | --- |
| `standalone` | `uvicorn api.main:app --port 8000` | navegador (também serve o SPA) |
| `full` | mesmo comando | gateway NestJS via `/internal/v1` |

## Instalação

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"        # Linux/macOS: .venv/bin/pip
# extras: .[ocr] .[queue] .[s3] .[postgres] .[docling] .[camelot] .[full]
```

## Execução

```bash
.venv/Scripts/python -m uvicorn api.main:app --port 8000
```

- API pública: `/api/v1` (contrato em `docs/API.md`)
- API interna: `/internal/v1` (token `CONVERTER_INTERNAL_TOKEN`)
- Docs interativas: `/docs`
- Health: `/health` e `/internal/v1/health`

## CLI

```bash
.venv/Scripts/python -m worker.cli documento.pdf -o saida.xlsx
```

## Migrações

```bash
.venv/Scripts/python -m alembic upgrade head
```

Na inicialização, o motor chama `init_db()` em qualquer modo: ele cria as tabelas
ausentes e aplica alterações aditivas. Alembic (`alembic upgrade head`) é a
alternativa versionada para ambientes controlados.

## Testes

```bash
.venv/Scripts/python -m pytest tests -q          # Linux/macOS: .venv/bin/python
.venv/Scripts/python scripts/smoke_run.py        # inspeção manual por fixture
```

A quantidade de testes muda com o código; o resumo do pytest é a referência.
Testes que dependem de Ollama ou do GLM-OCR são pulados quando o serviço ou o
modelo não existem.

## Estrutura

```
api/            FastAPI (rotas públicas e internas, SSE, auth)
engine/         pipeline e todos os estágios
audit/          eventos encadeados, cobertura, evidência, manifesto
models/         IR universal e schema SQLAlchemy
repositories/   escrita/leitura da proveniência
storage/        local | S3
jobqueue/       inline | celery
worker/         execução, tarefas Celery, CLI
migrations/     Alembic
tests/          fixtures geradas + suítes
```

## Docker

Construa a partir da raiz do repositório, porque o pacote usa arquivos fora de
`services/converter`:

```bash
docker build -f services/converter/Dockerfile -t pdf2x-converter .
```

A imagem (`python:3.12-slim`) instala Tesseract com o pacote de idioma português,
as bibliotecas do OpenCV e os extras `full` e `postgres`. Ela executa como usuário
sem privilégios (uid 10001). Os caminhos padrão são relativos a `/app`; em produção,
defina caminhos absolutos para o volume persistente. O GLM-OCR não está na imagem:
ele depende de um Ollama alcançável (`GLMOCR_HOST`/`GLMOCR_PORT`). Veja
[`../../docs/DEPLOY.md`](../../docs/DEPLOY.md).

Detalhes do pipeline: [`../../docs/CONVERSION.md`](../../docs/CONVERSION.md).
