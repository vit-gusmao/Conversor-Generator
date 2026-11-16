# Arquitetura

## Visão de 30 segundos

```
 navegador
    │  upload / SSE / download
    ▼
 apps/web (React)  ──────────────►  apps/api (NestJS)
                                        │  JWT, ownership, idempotência
                                        │  SSE + fila de eventos
                                        ▼
                              services/converter (FastAPI)
                                   │            │
                            fila (inline|Celery) │
                                   ▼            ▼
                              pipeline Python  storage (local|S3)
                                   │
                                   ├── PostgreSQL/SQLite: proveniência
                                   └── XLSX + manifesto de auditoria
```

Dois modos, o mesmo contrato:

| Modo | Quem responde `/api/v1` | Banco | Fila | Arquivos |
| --- | --- | --- | --- | --- |
| `standalone` | FastAPI | SQLite | thread pool | filesystem |
| `full` | NestJS | PostgreSQL (2 bancos) em produção; SQLite também suportado pela API | `inline` ou Celery/Redis | disco local ou S3/MinIO |

O modo `standalone` existe para que o produto rode inteiro com um processo —
inclusive servindo o SPA compilado — sem perder nenhuma regra de negócio. As
duas implementações da API compartilham o mesmo `ConversionService`, os mesmos
tipos (`packages/contracts`) e os mesmos testes de contrato.

## Fronteira de escrita

```
NestJS escreve        Python escreve
──────────────────    ────────────────────────────────
users                 runs
documents             page_results, regions, text_elements
conversions           extracted_fields, extracted_tables
conversion_runs       output_sheets, output_cells, cell_evidence
conversion_events     audit_events, audit_issues
outbox_events         validation_results, coverage_rows
idempotency_keys      engine_usage, stage_timings, artifacts
```

Em `full` são **dois bancos** (`pdf2x_api`, `pdf2x_conv`); nenhuma tabela é
escrita por dois ORMs. O gateway lê a auditoria via API interna e a repassa ao
navegador — nunca acessa o banco do motor.

## Por que estas decisões

**Modelo intermediário único.** Nenhuma biblioteca escreve no XLSX. pdfplumber,
OCR, campos e tabelas produzem `Token`, `TextBlock`, `Field`, `Record`,
`TableStructure`; o resolvedor produz `WorkbookPlan`; só o writer toca em
`openpyxl`. Trocar um extrator não muda a saída.

**Motor de campos próprio.** Nenhuma biblioteca converte `Nome: João da Silva`
em uma coluna `Nome`. É o diferencial do produto, então é código nosso — com
heurísticas explicáveis e códigos de razão na auditoria.

**pdfplumber como base textual.** Mantém coordenadas por caractere, fontes e
tamanhos — o que um `extract_text()` destrói. PyMuPDF foi evitado de propósito
por causa da licença AGPL em produto proprietário.

**Camelot/Docling opcionais.** Camelot exige Ghostscript e Docling baixa
modelos; ambos entram como adaptadores. A rota nativa (lattice/stream/híbrido)
resolve a maioria dos casos sem dependências pesadas.

**Fila abstrata.** `inline` usa um pool de threads com cancelamento; `celery`
usa Redis. O pipeline é idêntico, então a troca é uma variável de ambiente.

**Nenhuma imagem.** A proibição é estrutural: o writer nunca chama
`add_image`, o plano não possui tipo de célula de imagem e o auditor verifica o
ZIP final (`xl/media/`, relacionamentos de desenho, `ws._images`).

## Fluxo de uma conversão (modo full)

```
1  POST /api/v1/conversions            → 202 {conversion_id, links}
2  Node: valida magic bytes, limite, hash SHA-256, grava PDF no storage
3  Node: cria Document + Conversion + Run + OutboxEvent
4  Node → FastAPI: POST /internal/v1/jobs/{runId}/enqueue
5  FastAPI: cria Run, despacha para a fila (inline/celery)
6  Worker: baixa o PDF, executa o pipeline, persiste proveniência
7  Node: poller (800ms) e/ou push hook atualizam status e publicam SSE
8  Navegador: recebe eventos (com retomada por Last-Event-ID)
9  GET .../result | /preview | /download | /audit
```

O caminho 7 aceita as duas vias de propósito: o hook é mais rápido, o poller é a
rede de segurança se o worker cair antes de notificar.

## Estrutura de código

```
services/converter/
  api/            main.py, public_routes.py, internal_routes.py, service.py, deps.py
  engine/
    inspector/    validação, criptografia, AcroForm, conteúdo ativo
    extraction/   texto nativo + qualidade da camada textual
    ocr/          rasterização, OpenCV, motores, reconciliação
    layout/       linhas, blocos, ordem de leitura, regiões
    fields/       rótulo/valor, registros
    tables/       lattice, stream, híbrido, Docling, builder
    structure/    resolver → WorkbookPlan
    normalization/ locale e tipos
    reconciliation/ continuação multipágina
    validation/   checagens pré-exportação
    excel/        writer + auditor
    pipeline.py   orquestração e progresso
  audit/          events, coverage, evidence, manifest
  models/         document.py (IR), db.py (SQLAlchemy)
  repositories/   persistência e leitura
  storage/        local, s3
  jobqueue/       base, inline, celery
  worker/         execução, Celery, CLI
  migrations/     Alembic
```

## Observabilidade

- Cada fase emite `STAGE_STARTED/COMPLETED` com duração → `stage_timings`.
- `GET /api/v1/conversions/{id}/audit` expõe tempos, motores, decisões e
  validações.
- Logs estruturados (`LOG_JSON=1`) carregam `run_id`, `stage`, `page`.
- `infra/observability/otel-collector.yaml` está pronto para receber OTLP; o
  encadeamento de trace entre Node/FastAPI/worker é a evolução natural
  (instrumentação explícita não é necessária para a auditoria funcional, que é
  persistida em banco).
