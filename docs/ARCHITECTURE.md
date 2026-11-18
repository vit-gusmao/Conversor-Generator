# Arquitetura

> **Sistema local.** O produto roda inteiro na máquina do usuário. A API e a
> interface escutam em `127.0.0.1` por padrão, o Docker Compose publica apenas a
> web em `127.0.0.1` e não há chamadas de saída, exceto integrações locais
> opcionais (Ollama, MinIO/S3 e o callback interno do motor).
>
> - Onde cada dado fica (banco, storage, logs): [STORAGE.md](STORAGE.md)
> - Fluxo completo de uma conversão, da seleção do arquivo ao download: [FLOW.md](FLOW.md)

## Visão geral

```
 navegador (127.0.0.1)
    │  upload / SSE / download
    ▼
 apps/web (React, SPA)  ──────────────►  apps/api (NestJS)          modo full
                                            │  identidade, ownership, idempotência
                                            │  SSE + fila de eventos
                                            ▼
                                  services/converter (FastAPI)
                                       │            │
                                fila (inline|Celery) │
                                       ▼            ▼
                                  pipeline Python  storage (local|S3)
                                       │
                                       ├── SQLite/PostgreSQL: proveniência
                                       └── XLSX/TXT/CSV + manifesto de auditoria

 navegador ──────────────►  services/converter (FastAPI, também serve o SPA)   modo standalone
```

| Modo | Quem responde `/api/v1` | Banco | Fila | Arquivos |
| --- | --- | --- | --- | --- |
| `standalone` | FastAPI (`services/converter/api`) | SQLite (`services/converter/data/converter.db`) | `inline` (pool de threads) | disco local (`services/converter/data/storage`) |
| `full` | NestJS (`apps/api`), com o FastAPI como motor interno | Gateway: Prisma (SQLite ou PostgreSQL). Motor: SQLite ou PostgreSQL | `inline` ou Celery/Redis | disco local ou S3/MinIO |

No Docker Compose o modo `full` usa PostgreSQL com dois bancos no mesmo cluster
(`pdf2x_api` e `pdf2x_conv`), Redis e MinIO. Os detalhes de bancos e arquivos
estão em [DATABASE.md](DATABASE.md) e [STORAGE.md](STORAGE.md).

O modo `standalone` roda o produto com um único processo, inclusive servindo o
SPA compilado. Os dois modos expõem o mesmo contrato HTTP (ver
[API.md](API.md)). Eles compartilham os tipos de `packages/contracts` e as
regras de opções, que existem em duas implementações espelhadas
(`packages/contracts/src/options.ts` e `services/converter/api/service.py`),
cobertas pelos testes de paridade de cada lado. Erros que não batem entre os
modos estão listados em [API.md](API.md#códigos-de-erro).

## Fronteira de escrita

Cada tabela tem um único escritor. O gateway (NestJS) não escreve no banco do
motor e o motor não escreve no banco do gateway.

| NestJS (Prisma), modo `full` | Python (SQLAlchemy), sempre | Python, só `standalone` |
| --- | --- | --- |
| `User`, `Document`, `Conversion`, `ConversionRun` | `runs`, `page_results`, `regions`, `text_elements` | `public_users`, `public_documents` |
| `ConversionEvent`, `OutboxEvent`, `IdempotencyKey` | `extracted_fields`, `extracted_tables`, `output_sheets`, `output_cells`, `cell_evidence` | `public_conversions`, `public_events` |
| `Batch`, `BatchItem`, `UploadSession`, `UploadPart`, `BatchEvent` | `audit_events`, `audit_issues`, `validation_results`, `coverage_rows` | `public_batches`, `public_batch_items`, `public_upload_sessions`, `public_upload_parts`, `public_batch_events` |
| | `engine_usage`, `stage_timings`, `artifacts`, `export_artifacts` | `idempotency_keys` (do fluxo público) |
| | `page_checkpoints`, `run_checkpoints`, `engine_jobs` | |
| | `knowledge_collections`, `knowledge_chunks`, `knowledge_index_jobs`, `conversations`, `chat_messages` | |

Em `full` o gateway lê a auditoria pela API interna do motor e a repassa ao
navegador. As tabelas `public_*` só recebem escrita quando as rotas públicas do
FastAPI são chamadas diretamente, o que no compose não acontece: o motor não
publica porta.

## Por que estas decisões

**Dois escritores, dois bancos.** Dois ORMs na mesma tabela significam duas
fontes de verdade sobre o schema. O gateway conhece aplicação (quem pediu,
quando, qual arquivo). O motor conhece evidência (o que foi lido, decidido e
escrito). Separar elimina migrações conflitantes e permite escalar o motor de
forma independente.

**Modelo intermediário único.** Nenhuma biblioteca escreve no XLSX. O
pdfplumber, o OCR, os campos e as tabelas produzem `Token`, `TextBlock`,
`Field`, `Record` e `TableStructure`. O resolvedor produz `WorkbookPlan` e só o
writer toca em `openpyxl`. Trocar um extrator não muda a saída.

**Motor de campos próprio.** Nenhuma biblioteca converte `Nome: João da Silva`
em uma coluna `Nome`. Esse é o diferencial do produto, então é código do projeto,
com heurísticas explicáveis e códigos de razão na auditoria.

**pdfplumber como base textual.** Mantém coordenadas por caractere, fontes e
tamanhos, que um `extract_text()` destrói. PyMuPDF não é usado de propósito por
causa da licença AGPL em produto proprietário.

**Camelot e Docling são opcionais.** Camelot exige Ghostscript e Docling baixa
modelos, então ambos entram como adaptadores opcionais (extras `camelot` e
`docling` do `pyproject.toml`). A rota nativa (lattice, stream e híbrido) resolve
a maioria dos casos sem dependências pesadas.

**Fila abstrata.** `inline` usa um pool de threads com cancelamento. `celery`
usa Redis. O pipeline é o mesmo nos dois casos, então a troca é uma variável de
ambiente (`CONVERTER_QUEUE`).

**Nenhuma imagem no XLSX.** A proibição é estrutural. O writer nunca chama
`add_image`, o `WorkbookPlan` não tem tipo de célula de imagem, e o auditor verifica o
pacote final (`xl/media/`, relacionamentos de desenho e `a:blip`/`r:embed`).

## Fluxo de uma conversão (modo full)

Resumo. O passo a passo com estados, eventos e pontos de falha está em
[FLOW.md](FLOW.md).

```
1  POST /api/v1/conversions            → 202 {conversion_id, links}
2  Node: valida magic bytes, limite, hash SHA-256, grava PDF no storage
3  Node: cria Document + Conversion + Run + OutboxEvent
4  Node → FastAPI: POST /internal/v1/jobs/{runId}/enqueue
5  FastAPI: cria Run, despacha para a fila (inline/celery)
6  Worker: baixa o PDF, executa o pipeline, persiste proveniência
7  Node: poller (CONVERTER_POLL_INTERVAL_MS, 800 ms por padrão) e/ou hook push
   (/api/v1/internal/hooks/converter) atualizam status e publicam SSE
8  Navegador: recebe eventos (com retomada por Last-Event-ID)
9  GET .../result | /preview | /download | /audit
```

O passo 7 aceita as duas vias de propósito. O hook é mais rápido e o poller é a
rede de segurança se o worker cair antes de notificar.

## Estrutura de código

```
services/converter/
  api/            main.py, public_routes.py, internal_routes.py, service.py, deps.py,
                  batch_routes.py, batch_service.py, upload_routes.py, upload_service.py,
                  knowledge_routes.py, schemas.py, capabilities.py
  engine/
    pipeline.py   orquestração, progresso e cancelamento
    inspector/    validação, criptografia, AcroForm, conteúdo ativo
    extraction/   texto nativo e qualidade da camada textual
    ocr/          rasterização, OpenCV, motores (GLM-OCR via Ollama), reconciliação
    layout/       linhas, blocos, ordem de leitura, regiões
    fields/       rótulo/valor, registros
    tables/       lattice, stream, híbrido, adaptadores Docling/Camelot
    structure/    resolvedor → WorkbookPlan
    normalization/ locale e tipos
    reconciliation/ continuação multipágina
    validation/   checagens pré-exportação
    excel/        writer e auditor do XLSX
    streaming/    leitura e escrita em fluxo para documentos grandes
  exports/        TXT, CSV, ZIP (base, registry, text_plan, text_writer, csv_writer)
  ingestion/      entrada de imagens (base, registry, image_input, image_pipeline)
  knowledge/      RAG: chunker, indexer, retrieval, repository
  assistant/      assistente local: service, ollama_client
  audit/          events, coverage, evidence, manifest
  models/         document.py (IR), db.py (SQLAlchemy)
  repositories/   conversion_repository, checkpoint_repository
  storage/        base, local, s3
  jobqueue/       base, inline, celery_queue, repository (engine_jobs), scheduler
  worker/         celery_app, celery_tasks, tasks, cli
  migrations/     Alembic (versions/)
  tests/          suíte pytest

apps/api/src/
  auth/           identidade de dispositivo, guards, JWT
  common/         ApiError, códigos de erro, filtros, interceptors
  config/         configuração tipada (variáveis de ambiente)
  database/       módulo Prisma
  upload/         validação de arquivo, storage local e S3
  modules/
    audit/        proxy da auditoria do motor
    batches/      lotes e itens
    conversions/  conversões, outbox, cliente do motor
    engine-proxy/ capacidades, exports, texto, conhecimento e assistente (proxy)
    health/       /health
    realtime/     SSE e hook de callback do motor
    settings/     /settings
    uploads/      envio por partes

apps/web/src/
  pages/          home, conversão, lote, resultado, histórico, auditoria, assistente, configurações
  services/       clientes HTTP por domínio (conversions, batches, uploads, audit, knowledge, assistant)
  stores/         estado de sessão, UI e fila de upload
  hooks/          React Query e SSE (conversões, lotes, auditoria, resultados)
  components/     componentes de domínio e de layout
  lib/            cliente HTTP, erros, hash SHA-256, validação de arquivo
  test/           configuração de testes

apps/mcp/src/
  tools/          ferramentas MCP (submit-files, get-batch, seal-batch, download-artifact, ...)
  api-client.ts   cliente da API com identidade de dispositivo
  transfer-worker.ts, submission-store.ts, path-policy.ts, identity.ts

packages/contracts/src/
  index.ts        status, tipos de conversão e resultado
  options.ts      ConversionOptionsV2 e normalizador
  batches.ts      contratos de lote
  uploads.ts      contratos de envio por partes
```

## Lotes

Um lote (`batch`) é um contêiner durável para um número sem teto de documentos.
Cada item vira exatamente uma conversão. O lote nunca funde arquivos em uma
única planilha. Em `full` as tabelas `Batch` e `BatchItem` vivem no banco do
gateway (Prisma). Em `standalone` vivem no banco do FastAPI, com prefixo
`public_`.

O status do item é derivado. Os estados de upload (`pending_upload`,
`uploading`, `upload_failed`, `cancelled`) vêm da própria linha do item. Depois
que a conversão existe, o status da conversão é a fonte da verdade. Não há um
segundo reconciliador de estados.

## Envio por partes

O upload de um arquivo é dividido em partes (8 MiB por padrão, em
`UPLOAD_PART_BYTES`). Cada parte é gravada numa área de staging do storage e
tem SHA-256 próprio. A conclusão confere contiguidade, tamanho total e o SHA-256
do documento antes de criar a conversão. O tamanho de parte é um parâmetro de
transporte e não limita o tamanho do documento. O limite de tamanho é
`UPLOAD_MAX_BYTES`, 50 MiB por padrão. Os contratos da API estão em
[API.md](API.md#lotes-e-envio-por-partes).

## Fila durável (`engine_jobs`)

- O backlog fica no banco. Não existe uma lista ilimitada de futures em memória.
- `submit()` grava a linha e acorda o scheduler. O pool recebe apenas trabalho que
  cabe nos slots livres (`CONVERTER_WORKERS`, 2 por padrão).
- `claim()` é um compare-and-set (`UPDATE ... WHERE state = 'queued'`) e
  incrementa o **fence**. Um worker que perdeu o lease não publica: `complete`,
  `heartbeat` e `fail` exigem o fence atual.
- Lease de 120 s (`LEASE_SECONDS`), renovado a cada 30 s (`HEARTBEAT_SECONDS`).
  Lease expirado volta para `queued`.
- Cancelamento é durável (`runs.cancel_requested_at` e
  `engine_jobs.cancel_requested_at`). O worker consulta entre páginas e a
  transação de publicação recusa o commit depois do pedido. O motor decide quem
  vence, não a API.
- `CONVERTER_TASK_TIMEOUT_SECONDS=0` (padrão) remove o prazo global do documento.
  O timeout passa a valer por unidade e por chamada de OCR.

## Checkpoints por página

Cada página extraída é serializada (tokens, blocos, regiões, tabelas) em
`runs/{run_id}/pages/extract/NNNNNN.json.gz` no storage, com SHA-256 registrado
na linha de `page_checkpoints`.

- Reuso só acontece quando a mesma execução é reentregue (tentativa maior que 1)
  e quando `source_sha256`, `engine_version`, `options_hash` e `schema_version`
  coincidem. Um retry pedido pelo usuário cria outro run e reprocessa tudo.
- Artefato corrompido (hash divergente) invalida apenas aquela página.
- Página com falha nunca é restaurada: é tentada de novo.

## Memória

- O PDF não é carregado inteiro. `materialize()` entrega um caminho em disco (sem
  cópia no backend local), e o SHA-256 é calculado em streaming.
- A inspeção é feita por caminho (`inspect_pdf_path`). Acima de 64 MiB, a
  varredura de conteúdo ativo é feita em blocos.
- O preview pagina em SQL: só a janela pedida de linhas é carregada.
- O XLSX é montado em memória pelo openpyxl antes de ir para o storage. Esse é o
  principal consumo de memória em documentos muito grandes (ver
  [Limitações conhecidas](#limitações-conhecidas)).

## Observabilidade

- Cada fase emite os eventos `STAGE_STARTED` e `STAGE_COMPLETED`. A duração é
  persistida em `stage_timings` (`duration_ms`).
- `engine_usage` guarda, por run, os motores usados e suas contagens.
- `audit_events` guarda a trilha da conversão, encadeada por hash, e
  `audit_issues` guarda avisos e erros. A auditoria é consultada em
  `GET /api/v1/conversions/{id}/audit`.
- Logs vão para a saída padrão (stdout), nos dois lados. No motor, `LOG_LEVEL`
  controla o nível (`INFO` por padrão) e `LOG_JSON=1` troca o formato para JSON.
  Os registros carregam `run_id`, `conversion_id`, `stage`, `page`, `event_type` e
  `duration_ms` quando existem. O gateway usa o `Logger` do NestJS e o
  interceptor de log redige valores sensíveis em query string (`access_token`,
  `device_id`, `token`, `secret` e similares).
- Procedimentos de diagnóstico, leitura de logs e health checks estão em
  [OPERATIONS.md](OPERATIONS.md).
- Health checks: `GET /api/v1/health` (gateway) devolve o estado de `db`, `queue`,
  `storage` e `converter`. Em `standalone`, `GET /api/v1/health` devolve os mesmos
  campos pelo FastAPI, e `GET /api/v1/health/ready` diz se a instância aceita
  novos envios e se o OCR está disponível, sem expor hosts ou caminhos.
- Não há exportação OpenTelemetry. Nenhum código lê as variáveis `OTEL_*` e não
  existe coletor OTLP configurado. A auditoria funcional é persistida em banco,
  que é a fonte de verdade para diagnóstico.

## Rede e limites de confiança

| Superfície | Bind padrão | Quem acessa | Proteção |
| --- | --- | --- | --- |
| Web (compose) | `127.0.0.1:${WEB_PORT:-8080}` | navegador local | publicada só em loopback |
| API NestJS | `API_HOST=127.0.0.1`, porta `API_PORT` | navegador local, web via proxy `/api/` | identidade de dispositivo ou JWT |
| FastAPI standalone | `uvicorn api.main:app --port 8000` (host padrão do uvicorn: `127.0.0.1`) | navegador local | identidade de dispositivo ou JWT |
| Motor (`/internal/v1/*`) | sem porta publicada no compose | apenas a API | header `x-internal-token` |
| Hook do motor (`/api/v1/internal/hooks/converter`) | mesmo bind da API | apenas o motor | header `x-internal-token` |

Notas:

- No contêiner o bind é `0.0.0.0` (`API_HOST` e o `--host` do Dockerfile do
  motor), mas o compose só publica a web, e só em `127.0.0.1`. Fora do contêiner,
  mantenha `API_HOST` em `127.0.0.1`.
- Variáveis de bind, token e origens estão listadas em [CONFIGURATION.md](CONFIGURATION.md).
- `CONVERTER_INTERNAL_TOKEN` protege a comunicação entre API e motor. O
  compose exige que a variável seja definida (`:?set`). O valor padrão
  `dev-internal-token-change-me` existe só para desenvolvimento, e a
  configuração do motor o lista como inseguro.
- A identidade de dispositivo (`x-device-id`, cookie `pdf2x_device_id` e JWT
  emitido para ela) separa dados entre navegadores. Não é um mecanismo de
  autenticação forte: quem tem o identificador do dispositivo acessa os dados
  daquele dispositivo. O modelo assume uso local por uma pessoa. Não existe
  limitação de taxa (rate limit) no gateway.
- CORS: `ALLOWED_ORIGINS` (padrão `http://localhost:8080` no compose).
- Chamadas de saída, todas locais por padrão:
  - Ollama, para o assistente (`ASSISTANT_HOST`, padrão `127.0.0.1`) e para o
    GLM-OCR (`GLMOCR_HOST`:`GLMOCR_PORT`, padrão `127.0.0.1:11434`). Se apontado
    para outra máquina, os dados saem dela.
  - MinIO ou S3, só com `STORAGE_BACKEND=s3`.
  - Redis, só com a fila `celery`.
  - Callback do motor para a API (`CONVERTER_CALLBACK_URL`). No compose é
    `http://api:3000`, dentro da rede do Docker.
  - Adaptadores opcionais (Docling) podem baixar pesos de modelo na primeira
    utilização, se instalados.

## Limitações conhecidas

Registradas para que nenhuma capacidade descrita acima seja lida como garantia
além do que o código faz:

1. **Estruturação em memória.** A extração é incremental e persistida por página,
   mas as etapas seguintes (campos, agrupamento de registros, resolução de abas)
   ainda constroem o documento inteiro em memória a partir dos checkpoints. Não
   há passagens persistidas em disco ou banco nessa etapa. O tempo e o disco são
   proporcionais ao documento e não há truncamento, mas o pico de RSS cresce com
   o número de páginas. Medição pontual feita antes desta revisão, em PDF nativo
   sintético com um marcador por página e 8 vCPU: 10.000 páginas em 226,8 s
   (44,1 páginas/s), pico de 1.668 MB de RSS, sem páginas com falha. Essa
   medição não tem um teste automatizado correspondente no repositório.
2. **XLSX montado em memória.** O openpyxl serializa o workbook a partir de um
   objeto em memória antes de gravá-lo no storage. A leitura para download já é
   em streaming, mas o pico do writer cresce com o número de células. Um
   `write_only` mudaria recursos usados (mesclagens, congelamento de painéis e
   filtro automático) e exige um teste de paridade do artefato antes de ser
   adotado.
3. **OCR em volume.** Processar 10.000 páginas com OCR real depende de GPU e de
   modelo local e não faz parte da suíte automatizada.
4. **S3/MinIO e Celery sem teste de integração.** Os backends existem e a lógica
   de partes foi exercitada no backend local. O repositório não tem testes de
   integração com S3/MinIO nem com Celery/Redis.
5. **Hash do arquivo inteiro no navegador.** O Web Crypto não oferece digest
   incremental, então um arquivo muito grande exige um buffer completo no
   cliente. Quando o navegador recusa, o envio falha com
   `ARQUIVO_MUITO_GRANDE_PARA_HASH_LOCAL` em vez de enviar um hash errado.
