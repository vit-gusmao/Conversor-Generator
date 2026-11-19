# Banco de dados

Onde cada banco fica e o que contém. Para o restante dos dados (PDFs, XLSX,
artefatos e logs), ver [STORAGE.md](STORAGE.md). As variáveis `API_DATABASE_URL`,
`CONVERTER_DATABASE_URL` e `API_POSTGRES_URL` estão descritas em
[CONFIGURATION.md](CONFIGURATION.md).

## Bancos e arquivos

| Ambiente | Gateway (NestJS, Prisma) | Motor (FastAPI, SQLAlchemy) |
| --- | --- | --- |
| `standalone` | não se aplica | SQLite em `services/converter/data/converter.db` (com `-wal` e `-shm`) |
| `full`, desenvolvimento local | SQLite em `apps/api/prisma/data/api.db` | SQLite em `services/converter/data/converter.db` |
| `full`, Docker Compose | PostgreSQL, banco `pdf2x_api`, schema `public` | PostgreSQL, banco `pdf2x_conv` |

Notas:

- O caminho do SQLite do motor é relativo a `services/converter/`
  (`CONVERTER_DATABASE_URL`, padrão `sqlite:///./data/converter.db`).
- O caminho do SQLite do gateway é `API_DATABASE_URL` (padrão `file:./data/api.db`).
  O Prisma resolve caminhos `file:` relativos ao arquivo de schema, e por isso o
  arquivo fica em `apps/api/prisma/data/api.db`, e não em `apps/api/data/`. A pasta
  é criada no primeiro `db push`.
- No Compose, `infra/postgres/init.sql` cria os bancos `pdf2x_api` e `pdf2x_conv`
  no cluster. O script roda só na primeira inicialização do volume `pgdata`. As
  URLs são montadas a partir de `POSTGRES_USER` e `POSTGRES_PASSWORD`.
- O `.env.example` mostra um exemplo com `?schema=api` e banco `pdf2x`, que não é
  o usado pelo Compose. Quando os dois divergirem, o Compose vale.
- Ao iniciar, o SQLite do motor recebe `PRAGMA journal_mode=WAL` e
  `PRAGMA foreign_keys=ON`.

## Fronteira entre os bancos

Cada tabela tem um único escritor. Os dois ORMs nunca escrevem na mesma tabela.
Por que a separação existe: ver [ARCHITECTURE.md](ARCHITECTURE.md#por-que-estas-decisões).

**Gateway (Prisma).** Os modelos não têm `@@map`, então as tabelas físicas têm o
nome do modelo.

| Modelo | Conteúdo |
| --- | --- |
| `User` | Identidade de dispositivo (`deviceId`) e dono |
| `Document` | PDF original: hash, tamanho, chave no storage |
| `Conversion` | Conversão do usuário, com `deletedAt` (exclusão lógica) |
| `ConversionRun` | Execução de uma conversão. Uma por tentativa |
| `ConversionEvent` | Eventos de progresso, com sequência por conversão |
| `OutboxEvent` | Fila de saída para o motor, com lease (`lockedUntil`) |
| `IdempotencyKey` | Chave `Idempotency-Key` por dono |
| `Batch`, `BatchItem` | Lote e itens (um item aponta para uma conversão) |
| `UploadSession`, `UploadPart` | Envio por partes e partes confirmadas |
| `BatchEvent` | SSE agregado do lote |

**Motor (SQLAlchemy).** Os 37 modelos de `services/converter/models/db.py`.

| Grupo | Tabelas |
| --- | --- |
| Execução | `runs`, `run_checkpoints`, `engine_jobs`, `page_checkpoints` |
| Extração | `page_results`, `regions`, `text_elements`, `extraction_candidates`, `decisions`, `extracted_fields`, `extracted_tables` |
| Saída | `output_sheets`, `output_cells`, `cell_evidence`, `artifacts`, `export_artifacts` |
| Auditoria | `audit_events`, `audit_issues`, `validation_results`, `coverage_rows` |
| Telemetria | `engine_usage`, `stage_timings` |
| Conhecimento (RAG) | `knowledge_collections`, `knowledge_chunks`, `knowledge_index_jobs` |
| Assistente | `conversations`, `chat_messages` |
| Fluxo público (só `standalone`) | `public_users`, `public_documents`, `public_conversions`, `public_events`, `idempotency_keys` |
| Lotes e uploads (só `standalone`) | `public_batches`, `public_batch_items`, `public_upload_sessions`, `public_upload_parts`, `public_batch_events` |

As tabelas `public_*` e `idempotency_keys` do motor só são escritas quando as
rotas públicas do FastAPI são chamadas. Em `full`, o motor não recebe chamadas
dessas rotas, porque o gateway é quem atende o cliente.

Em `full`, lotes e uploads vivem no Prisma (`Batch`, `BatchItem`, `UploadSession`,
`UploadPart`, `BatchEvent`). Em `standalone`, vivem em `public_batches`,
`public_batch_items`, `public_upload_sessions`, `public_upload_parts` e
`public_batch_events`.

## Tabelas principais do motor

| Tabela | Conteúdo | Chave |
| --- | --- | --- |
| `runs` | Execução, status, progresso, estatísticas, validação e `cancel_requested_at` | `id` (UUID) |
| `page_results` | Página, classificação, caracteres nativos e OCR, métricas | único `run_id, page_number` |
| `regions` | Regiões e seus tipos | `run_id, region_id` |
| `text_elements` | Blocos com bbox, fonte, origem e destino | `run_id, element_id` |
| `extracted_fields` | Rótulo, valor, confiança, separador | `run_id, record_id` |
| `extracted_tables` | Estratégia, cabeçalhos, linhas, mesclagens e alternativas | `run_id, table_id` |
| `output_sheets` | Abas, tipo, ordem e páginas de origem | `run_id, sheet_id` |
| `output_cells` | Célula, valor, original, tipo, formato e origem | único `run_id, sheet, cell_ref` |
| `cell_evidence` | Célula para elemento de origem | `run_id` |
| `audit_events` | Evento encadeado por hash | único `run_id, sequence` |
| `audit_issues` | Avisos e erros | `run_id` |
| `validation_results` | Checagens e medições | `run_id` |
| `coverage_rows` | Cobertura por página | único `run_id, page_number` |
| `engine_usage` | Motores usados e contagens | único `run_id, engine, kind` |
| `stage_timings` | Duração por fase (`duration_ms`) | `run_id` |
| `artifacts` | XLSX e manifesto no storage | `run_id, kind` |
| `export_artifacts` | TXT, CSV e ZIP exportados, com hash e `publication_status` | `run_id` |
| `page_checkpoints` | Uma página extraída: `source_sha256`, `engine_version`, `options_hash`, `schema_version`, `artifact_key`, `artifact_sha256`, `status` | único `run_id, page_number, stage` |
| `run_checkpoints` | Cursor por fase (`structure`, `export`) e última sequência de evento | único `run_id, phase` |
| `engine_jobs` | Backlog durável: `state`, `available_at`, `lease_owner`, `lease_until`, `fence`, `attempt`, `payload_ref`, `cancel_requested_at` | único `run_id, unit_key` |

Tabelas do domínio de fontes e automação (importação por caminho). Todas são
aditivas. Colunas acrescentadas depois, em `knowledge_index_jobs` e
`automation_runs`, estão em [Colunas aditivas](#colunas-aditivas).

| Tabela | Conteúdo | Chave/unicidade |
| --- | --- | --- |
| `source_roots` | Concessão de raiz: `owner_id`, `runtime_id`, `canonical_path`, `grant_channel`, `revoked_at` | `id` (índice `owner_id, revoked_at`) |
| `file_sources` | Fonte cadastrada: caminho relativo, tipo, filtros, recursividade, `version` | `id`; índice `owner_id, enabled` |
| `source_imports` | Execução de importação: `idempotency_key`, `request_hash`, `definition_json` (definição congelada), contadores, lease/fence | único `owner_id, idempotency_key` |
| `source_import_items` | Item por arquivo: caminho, sha256, snapshot, upload/conversão, estado, erros | único `import_id, client_item_id` |
| `source_file_versions` | Última versão bem-sucedida por arquivo lógico | único `owner_id, source_id, relative_path, sha256, options_hash` |
| `source_knowledge_heads` | Cabeça de cada arquivo lógico na coleção: `active_document_id`, `active_run_id`, `index_generation` (geração vigente) e `source_missing` (origem sumiu da fonte) | único `owner_id, collection_id, source_id, relative_path` |
| `automation_schedules` | Definição da agenda: regra, fuso, `revision`, `next_run_at`, políticas | `id`; índice `enabled, next_run_at` |
| `automation_runs` | Ocorrência: `occurrence_key`, snapshot, estado/fase, lease/fence, `cancel_requested_at`, `next_attempt_at` (horário mínimo de nova tentativa após falha transitória) | único global `occurrence_key` |
| `automation_events` | Linha do tempo por ocorrência | único `run_id, sequence` |

Os modelos de conhecimento e assistente:

| Tabela | Conteúdo |
| --- | --- |
| `knowledge_collections` | Coleção do dono, escopo, `deleted_at` |
| `knowledge_chunks` | Trechos indexados, com `deleted_at` e unicidade `owner_id, collection_id, chunk_id` |
| `knowledge_index_jobs` | Estado e estatísticas de cada job de indexação |
| `conversations`, `chat_messages` | Conversas do assistente, com `deleted_at` na conversa |

## Soft delete

Exclusão lógica usa a coluna `deleted_at` (ou `deletedAt`, no Prisma):

- Motor: `knowledge_collections`, `knowledge_chunks`, `conversations` e
  `public_conversions` (`standalone`).
- Gateway: `Conversion`.

Excluir uma conversão ou uma conversa do assistente marca a linha, sem apagá-la.
Excluir uma coleção de conhecimento remove a coleção e os seus chunks. Os arquivos
ligados a uma conversão excluída são removidos do storage conforme descrito em
[API.md](API.md#conversões) e [STORAGE.md](STORAGE.md).

## Índices

Os índices compostos seguem as consultas de filtro, sempre começando por
`run_id` ou pelo dono. Alguns exemplos, de `services/converter/models/db.py`:

- `output_cells`: `ix_output_cells_run_sheet` (explorador de células).
- `audit_events`: `ix_audit_events_stage`, `ix_audit_events_type` e
  `ix_audit_events_page` (filtros da auditoria).
- `text_elements`: `ix_text_elements_run_page` e `ix_text_elements_run_element`.
- `page_checkpoints`: `ix_page_checkpoints_run_stage`.
- `engine_jobs`: `ix_engine_jobs_state_available`, para a varredura do scheduler.
- `public_batches`, `public_batch_items`, `public_upload_sessions` e
  `public_upload_parts`: índices por dono, por lote e por estado de upload.

O Prisma declara índices equivalentes, por exemplo `@@index([ownerId, createdAt])` em
`Conversion` e `@@index([batchId, ordinal, id])` em `BatchItem`.

## Chaves únicas que sustentam a idempotência

| Tabela | Chave única | Efeito |
| --- | --- | --- |
| `Conversion` / `ConversionRun` (Prisma) | `ConversionRun(conversionId, attempt)` | uma linha por tentativa |
| `IdempotencyKey` (Prisma) | `(ownerId, key)` | a mesma chave devolve a mesma conversão |
| `Batch` (Prisma) | `(ownerId, clientBatchKey)` | a mesma chave devolve o mesmo lote |
| `BatchItem` (Prisma) | `(batchId, clientItemId)`; `uploadId` e `conversionId` também são únicos | o mesmo item não cria outra conversão |
| `UploadPart` (Prisma) | `(uploadId, partNumber)` | reenviar a mesma parte é uma operação sem efeito |
| `public_batches` | `(owner_id, client_batch_key)` | o mesmo que `Batch` no modo `standalone` |
| `public_batch_items` | `(batch_id, client_item_id)`; `upload_id` e `conversion_id` únicos | o mesmo que `BatchItem` |
| `public_upload_parts` | `(upload_id, part_number)` | o mesmo que `UploadPart` |
| `engine_jobs` | `(run_id, unit_key)` | uma unidade de trabalho por run |
| `page_checkpoints` | `(run_id, page_number, stage)` | um checkpoint por página e fase |

## Colunas aditivas

Alguns campos foram adicionados depois da primeira versão do schema. Todos são
nulos ou têm padrão, e não reescrevem dados antigos:

- `runs.cancel_requested_at` e `engine_jobs.cancel_requested_at`: arbitragem de
  cancelamento contra a publicação.
- `public_conversions.options_json`: passa a ser preenchido em toda conversão nova.
  Conversões anteriores ficam com `NULL` e usam os padrões do tipo.
- `knowledge_index_jobs.attempt`, `fence`, `lease_owner` e `lease_expires_at`
  (revisão `d5e0a7c31b92`): lease e fence dos jobs de indexação. Jobs anteriores
  recebem `attempt = 0` e `fence = 0`, sem lease.
- `automation_runs.next_attempt_at` (revisão `d5e0a7c31b92`): horário mínimo para
  a próxima tentativa de uma ocorrência com falha transitória. `NULL` para o resto.

## JSON

- No motor, payloads variáveis (métricas, contexto de issues e alternativas de
  tabela) usam colunas `JSON`. Campos filtrados com frequência são colunas
  indexadas, e nunca ficam dentro de JSON.
- No gateway, o Prisma guarda payloads JSON como texto (`String`) nos dois
  providers. O repositório usa helpers de serialização para isso.

## Migrações

Cadeia do motor (ordem): `a95827baa7d8` → `c4f1a2b7d910` → `6d13f972f6c5` →
`e08fecd3c89f` → `f2a8c61b9e73` (fontes e automação) →
`b7c4d2e91f38` (`automation_runs.cancel_requested_at`) →
`d5e0a7c31b92` (lease e fence dos jobs de indexação e `automation_runs.next_attempt_at`).
**A revisão-cabeça é `d5e0a7c31b92`.** Todas aditivas; o standalone continua
funcionando com `create_all` e as mesmas tabelas.

| Serviço | Ferramenta | Como aplicar |
| --- | --- | --- |
| Motor | `init_db()` na inicialização, em qualquer modo | cria as tabelas ausentes e aplica colunas e índices aditivos faltantes |
| Motor, desktop empacotado | `runtime/db_migrate.py` (sem Alembic) | trava de arquivo, backup do SQLite antes de mudar o schema, `init_db()` e gravação manual de `d5e0a7c31b92` em `alembic_version` |
| Motor | Alembic, versionado | `pip install -e ".[migrate]"` em `services/converter`, depois `alembic upgrade head` |
| Gateway | Prisma `db push`, sem migrações versionadas | `npm run db:push` na raiz. No PostgreSQL, `apps/api/docker-entrypoint.sh` roda `prisma db push` a cada inicialização |

Detalhes:

- `init_db()` usa `create_all`, que não altera tabelas existentes. Por isso, em
  seguida, `_ensure_additive_columns` acrescenta, de forma genérica, toda coluna
  e índice que o modelo declara e a tabela não tem. Colunas nulas entram como
  estão. Colunas `NOT NULL` com padrão escalar entram com esse `DEFAULT`, para
  preencher as linhas antigas. Colunas `NOT NULL` sem padrão aproveitável entram
  como nulas. Não há remoção nem alteração de tipo. Alterações destrutivas ficam
  com o Alembic. Pode ser executado repetidamente.
- Cadeia do Alembic (`services/converter/migrations/versions/`):

  ```
  a95827baa7d8  initial_schema                       (sem pai)
  c4f1a2b7d910  batches_and_uploads                  ← a95827baa7d8
  6d13f972f6c5  knowledge_exports_and_conversion_options ← c4f1a2b7d910
  e08fecd3c89f  durable_jobs_and_page_checkpoints    ← 6d13f972f6c5
  f2a8c61b9e73  sources_and_automation               ← e08fecd3c89f
  b7c4d2e91f38  run_cancel_requested                 ← f2a8c61b9e73
  d5e0a7c31b92  index_job_leases_and_run_retry       ← b7c4d2e91f38   (head)
  ```

  A migração inicial foi gerada contra um banco vazio a partir de `models/db.py`.
  O `env.py` lê a URL de `CONVERTER_DATABASE_URL`. Depois de `upgrade head`,
  `alembic check` deve confirmar que não há operações pendentes.
- Gateway: `apps/api/prisma/schema.prisma` (SQLite) e
  `apps/api/prisma/schema.postgres.prisma` (PostgreSQL) têm os mesmos modelos.
  Quando alterar um, altere o outro. O `apps/api/Dockerfile` copia o schema de
  PostgreSQL para `schema.prisma` quando `DATABASE_PROVIDER=postgres`.
- O repositório não tem migrações do Prisma. Localmente, o `db push` é
  aplicado a cada inicialização do contêiner. Mudanças que removem colunas ou
  dados devem passar por uma revisão antes de serem aplicadas.
