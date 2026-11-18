# API — contrato local

Este é o contrato HTTP da aplicação, que roda na máquina do usuário. O mesmo
contrato é servido por dois processos. Escolha o endereço pelo modo em uso:

| Modo | Base URL | Quem responde |
| --- | --- | --- |
| `standalone` | `http://127.0.0.1:8000/api/v1` | FastAPI (`uvicorn api.main:app --port 8000`), que também serve o SPA |
| `full` (direto) | `http://127.0.0.1:3000/api/v1` | NestJS (`apps/api`), que encaminha ao motor FastAPI quando preciso |
| `full` (compose) | `http://localhost:8080/api/v1` | Nginx da web (`apps/web`), que faz proxy de `/api/` para a API NestJS |

Os limites citados aqui (`UPLOAD_MAX_BYTES`, `UPLOAD_PART_BYTES` etc.) são
variáveis de ambiente descritas em [CONFIGURATION.md](CONFIGURATION.md).

Todos os payloads são JSON, exceto upload, download e os fluxos SSE. Quando uma
rota existe só em um dos modos, a tabela ou a seção indica qual.

Os modos são espelhos, não cópias. A maioria dos endpoints e dos formatos é
igual, mas alguns códigos de erro e alguns status HTTP diferem. As diferenças
estão marcadas nas tabelas e resumidas em [Diferenças entre os modos](#diferenças-entre-os-modos).
Para o significado de cada modo, ver [ARCHITECTURE.md](ARCHITECTURE.md). Para
onde os arquivos vão, ver [STORAGE.md](STORAGE.md).

## Convenções

- Identificadores: UUID v4.
- Datas: ISO-8601 em UTC, com sufixo `Z`.
- Erros usam sempre o envelope `{ "error": { "code", "message", "details" } }`,
  com o status HTTP correspondente. A lista de códigos está em
  [Códigos de erro](#códigos-de-erro).
- Paginação: `?page=1&page_size=20`, com `page_size` máximo de 100. A resposta é
  `{ items, total, page, page_size }`.
- Idempotência: o cabeçalho `Idempotency-Key` em `POST /conversions`, e
  `client_batch_key` e `client_item_id` nos lotes.
- Eventos em tempo real: SSE. Para retomar, envie `Last-Event-ID`. Nos lotes, o
  gateway também aceita `?last_event_id=`.

## Identidade local

Não existe cadastro, login nem senha. Cada navegador recebe uma identidade de
dispositivo. O JWT emitido para ela é usado para escopar conversas, lotes,
artefatos e conversas com o assistente ao dono daquela identidade.

| Entrada | Onde | Uso |
| --- | --- | --- |
| `Authorization: Bearer <jwt>` | header | token emitido por `POST /auth/anonymous` |
| `x-device-id` | header | identificador do dispositivo. Também é devolvido em `x-device-id` na resposta |
| cookie `pdf2x_device_id` | cookie `HttpOnly` | mesma identidade, mantida pelo navegador |
| `?device_id=` | query | aceito só no gateway NestJS |
| `?access_token=<jwt>` | query | só em rotas SSE, porque `EventSource` não envia headers |

Ordem de resolução:

- `standalone`: Bearer, depois `x-device-id`, depois o cookie. Sem nenhum deles,
  o servidor cria um identificador (`dev_<hex>`) e o devolve pelo cookie.
- `full` (gateway): Bearer, depois `x-device-id`, depois `?device_id=`, depois o
  cookie. Sem nenhum deles, cria um identificador e o devolve em `x-device-id`.

O navegador envia `x-device-id` com o valor guardado em `localStorage`
(`pdf2x.device_id`). O cookie existe como segunda via, para o caso de o
`localStorage` ser limpo.

## Endpoints

### Sessão local

| Método | Endpoint | Descrição |
| --- | --- | --- |
| POST | `/auth/anonymous` | Cria ou retoma a sessão local do navegador. Devolve `AuthSession` (com `access_token`) e define o cookie. Status `201`. Com `AUTH_MODE=required` e sem token, o motor standalone devolve `401`; o NestJS responde `201` em qualquer modo. |
| GET | `/auth/me` | Identidade local atual, `User` |

### Conversões

Estas rotas existem nos dois modos. O gateway encaminha ao motor as que não são
PDF→XLSX legadas (exports, texto, capacidades e assistente).

| Método | Endpoint | Descrição |
| --- | --- | --- |
| POST | `/conversions` | `multipart/form-data` com o campo `file`, e opcionalmente `options` (JSON). Cabeçalho opcional `Idempotency-Key`. Retorna **202** `{conversion_id, status:"queued", message, links}`. Grava o PDF original no storage antes de criar a conversão ([STORAGE.md](STORAGE.md)) |
| GET | `/conversions` | Histórico do dono. Filtros: `status`, `q`, `page`, `page_size` |
| GET | `/conversions/:id` | Status detalhado, estatísticas e validação |
| GET | `/conversions/:id/events` | SSE de progresso. Aceita `Last-Event-ID` e `?access_token=` |
| GET | `/conversions/:id/result` | Metadados do resultado (`ResultSummary`) |
| GET | `/conversions/:id/result-summary` | Resumo compacto do resultado, ver [Resultado compacto](#resultado-compacto) |
| GET | `/conversions/:id/sheets` | Abas do resultado, paginadas, ver [Resultado compacto](#resultado-compacto) |
| GET | `/conversions/:id/sheets/:sheetId/pages` | Páginas de origem de uma aba, paginadas |
| GET | `/conversions/:id/preview` | `?sheet=&offset=&limit=` retorna `Preview` |
| GET | `/conversions/:id/pages` | Páginas processadas: dimensões, classificação, `text_source`, cobertura e número de regiões |
| GET | `/conversions/:id/tables` | Tabelas reconstruídas: cabeçalhos, linhas, estratégia, mesclagens e continuidade |
| GET | `/conversions/:id/cells/:cellId` | Origem de uma célula, endereçada como `Aba!C2` ou `C2`. Rastreabilidade do plano |
| GET | `/conversions/:id/download` | Baixa o `.xlsx` |
| GET | `/conversions/:id/exports` | Lista artefatos TXT/CSV/ZIP com hash, tamanho, avisos e `publication_status` |
| POST | `/conversions/:id/exports` | Cria um artefato a partir do IR armazenado: `{format:"txt"\|"csv", options}`. Status `201`. Grava o artefato no storage e registra o hash |
| GET | `/conversions/:id/exports/:exportId/download` | Baixa o artefato aprovado. Quarentena retorna 409 `ARTIFACT_QUARANTINED` |
| GET | `/conversions/:id/text` | Texto paginado: `?page=&offset=&limit=` |
| POST | `/conversions/:id/retry` | Cria um novo `run`. Status `202` |
| POST | `/conversions/:id/cancel` | Solicita cancelamento. Status `202` no standalone e `200` no gateway |
| DELETE | `/conversions/:id` | Exclusão lógica (`deleted_at`). Status `204`. O PDF original é removido do storage quando não é usado por outra conversão (gateway). No standalone, são removidos o `output.xlsx` e o `audit.json.gz` do run ativo |

### Capacidades e modelo

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/capabilities` | Entradas e codecs instalados, saídas (xlsx, txt, csv), limites e estado do OCR |
| GET | `/models/status` | Modelo configurado, disponibilidade, digest, contexto e concorrência. Sem segredos |

### Conhecimento (RAG)

| Método | Endpoint | Descrição | Modo |
| --- | --- | --- | --- |
| GET | `/knowledge/collections` | Coleções do dono, com contagem de chunks | ambos |
| POST | `/knowledge/collections` | `{name, description?, scope:"documents"\|"studies"}`. Status `201` | ambos |
| DELETE | `/knowledge/collections/:id` | Remove a coleção e seus chunks. Status `204` | ambos |
| POST | `/knowledge/index-jobs` | `{collection_id?, conversion_ids[], source_paths[]}`. Status `202` | ambos |
| GET | `/knowledge/index-jobs/:id` | Estado do job e estatísticas | ambos |
| POST | `/knowledge/search` | `{query, collection_id?, top_k?}`. Retorna trechos com citação e score | **somente standalone** |

`POST /knowledge/search` não é encaminhado pelo gateway. Em `full` a rota não
existe e responde `404 NOT_FOUND`. A consulta vazia retorna `400 INVALID_QUERY`.

### Assistente

Estas rotas existem nos dois modos. No gateway, o stream de mensagens é
repassado do motor.

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/chat/conversations` | Conversas do dono |
| POST | `/chat/conversations` | `{title?, collection_id?, document_id?, run_id?}`. Status `201` |
| GET | `/chat/conversations/:id` | Conversa com histórico e citações |
| DELETE | `/chat/conversations/:id` | Exclusão lógica. Status `204` |
| POST | `/chat/conversations/:id/messages` | SSE com os eventos `status`, `delta`, `citation`, `done` e `error` |

Quando o assistente falha, o evento `error` traz um código de modelo. Ver
[Códigos de eventos](#códigos-fora-do-http).

### Opções de conversão

O upload aceita o campo `options` (JSON) junto do arquivo:

```json
{
  "schema_version": 2,
  "target_formats": ["xlsx", "txt", "csv"],
  "ocr": { "enabled": true, "dpi": 300 },
  "text": { "newline": "lf", "page_separator": "form_feed" },
  "csv": { "profile": "interoperable", "formula_policy": "safe" }
}
```

A forma completa (`ConversionOptionsV2`) está em [Opções (`ConversionOptionsV2`)](#opções-conversionoptionsv2).

- Opções desconhecidas são rejeitadas com `INVALID_OPTION`.
- `max_pages`, `maxPages`, `page_limit` e `max_pages_per_document` são rejeitados
  com `INVALID_OPTION`. O fluxo padrão processa o documento inteiro.
- Imagens aceitam apenas `target_formats: ["txt"]`. Qualquer outro alvo é
  rejeitado. Ver [Códigos de erro](#códigos-de-erro).

### Auditoria

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/conversions/:id/audit` | `AuditSummary` |
| GET | `/conversions/:id/audit/events` | Eventos. Filtros: `stage`, `event_type`, `severity`, `page`, `since_sequence`, `limit` |
| GET | `/conversions/:id/audit/pages/:page` | `PageAudit` |
| GET | `/conversions/:id/audit/cells` | Proveniência. Filtros: `sheet`, `cell`, `limit`. `transformations` e `value_matches_source` são calculados a partir do par original e normalizado |
| GET | `/conversions/:id/audit/issues` | `{ items: AuditIssue[], total }` |
| GET | `/conversions/:id/audit/export` | `?format=json\|txt`, download |

A trilha é encadeada por hash no motor (`audit_events`). Ver [STORAGE.md](STORAGE.md)
para onde ela é gravada.

### Sistema

| Método | Endpoint | Descrição | Modo |
| --- | --- | --- | --- |
| GET | `/settings` | `PlatformSettings` | ambos |
| GET | `/health` | `{status, services:{db, queue, storage, converter}}` | ambos |
| GET | `/health/ready` | `{status, accepting_uploads, ocr_available, queue_pending}`. Não expõe hosts nem caminhos | **somente standalone** |

`/health/ready` responde se a instância aceita novos envios e se o OCR está
disponível. A interface atual não usa essa rota. Ela existe para clientes
externos e para diagnóstico. No gateway a rota não existe e responde `404`.

## Regras de estado

```
received → validating → queued → inspecting → extracting → structuring
        → normalizing → reconciling → generating → auditing
        → completed | completed_with_warnings | failed | cancelled
```

- Estados terminais não voltam a ser ativos. `retry` cria um novo `conversion_run`.
- `progress` vai de `0` a `1`. `progress_indeterminate=true` indica que a etapa
  não tem estimativa confiável.
- Nenhuma etapa publica dados finais sem registrar antes um evento de auditoria.

## Códigos de erro

Erros usam o envelope `{ "error": { "code", "message", "details" } }`. Os
códigos abaixo são os que o código emite, com o status HTTP e o modo.

Quando a coluna **Modo** diz `ambos`, o código e o status são iguais nos dois
processos. Quando diz `standalone` ou `gateway`, o outro modo usa outro código
ou outro status, descrito na própria linha.

### Validação de arquivo e opções

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 400 | `INVALID_FILE` | Arquivo vazio, que não é PDF nem imagem suportada, ou sem nome de arquivo no lote | ambos |
| 400 | `FILE_TOO_LARGE` | Acima de `UPLOAD_MAX_BYTES` (50 MiB por padrão); no envio simples do motor, acima de `min(CONVERTER_MAX_PDF_BYTES, UPLOAD_MAX_BYTES)`. Em partes, ver `413` | ambos |
| 400 | `TOO_MANY_PAGES` | PDF com mais páginas que `UPLOAD_MAX_PAGES`. Só existe quando a variável é definida explicitamente | gateway |
| 400 | `INVALID_OPTION` | Opção desconhecida, limite de páginas (`max_pages` etc.), JSON de `options` malformado, `target_formats` vazio, valor fora do enum, `scope` inválido em `retry` | ambos |
| 400 | `UNSUPPORTED_FORMAT` | `target_formats` com formato fora de `xlsx`, `txt` e `csv`, ou formato de exportação diferente de `txt` e `csv` | standalone. No gateway, o mesmo caso é `INVALID_OPTION` |
| 400 | `UNSUPPORTED_TARGET` | Imagem com `target_formats` diferente de `["txt"]` | standalone |
| 400 | `INCOMPATIBLE_OUTPUT_FORMAT` | Imagem com `xlsx` ou `csv`. Produzido pelo normalizador do contrato no gateway | gateway |
| 400 | `INVALID_CELL_ID` | Célula fora do formato `Aba!C2` ou `C2` | standalone. O gateway repassa o código do motor |
| 400 | `INVALID_QUERY` | Consulta vazia em `/knowledge/search` | standalone |
| 400 | `UPLOAD_PART_MISMATCH` | Número de parte fora da faixa `0..N-1` | ambos |
| 422 | `VALIDATION_ERROR` | Corpo ou parâmetros que não passam na validação de DTO | ambos |

### Autenticação e acesso

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 401 | `UNAUTHENTICATED` | JWT inválido ou expirado, ou rota que exige identidade sem ela | ambos |
| 401 | `UNAUTHORIZED_INTERNAL` | `x-internal-token` inválido nas rotas `/internal/v1` do motor | standalone e motor |
| 403 | `FORBIDDEN` | Recurso de outro dono | ambos |
| 404 | `NOT_FOUND` | Recurso inexistente, rota inexistente ou rota que o gateway não encaminha | ambos |
| 404 | `UPLOAD_NOT_FOUND` | Sessão de upload inexistente em `GET`, `PUT` ou `complete` | standalone. O gateway responde `NOT_FOUND` |

### Estado e concorrência

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 409 | `ALREADY_TERMINAL` | `retry` ou `cancel` numa conversão já terminal | ambos |
| 409 | `ALREADY_ACTIVE` | `retry` numa conversão ainda em andamento | standalone. O gateway responde `CONVERSION_BUSY` |
| 409 | `CONVERSION_BUSY` | Operação que exige conversão parada, com a conversão em andamento (retry no gateway; repetir item de lote) | ambos |
| 409 | `NOT_READY` | Resultado, XLSX ou prévia ainda não disponíveis, ou repetir item de lote sem conversão | ambos |
| 409 | `IDEMPOTENT_REPLAY` | Ver [Idempotência](#idempotência) | ambos |
| 409 | `IDEMPOTENCY_KEY_REUSED` | `Idempotency-Key` repetida com outro arquivo ou outras opções. Nunca devolve a conversão anterior | standalone |
| 409 | `BATCH_NOT_OPEN` | Lote selado, que não aceita mais itens | ambos |
| 409 | `BATCH_CANCELLED` | Lote cancelado | ambos |
| 409 | `BATCH_HAS_PENDING_UPLOADS` | Selar um lote com uploads em `pending_upload` ou `uploading`. Com a contagem em `details`. `upload_failed` não bloqueia | ambos |
| 409 | `UPLOAD_STATE_CONFLICT` | Sessão já concluída ou abortada, ou ação inválida para o estado | ambos |
| 409 | `UPLOAD_INCOMPLETE` | `complete` com partes faltando, total de partes diferente ou bytes divergentes | ambos |
| 409 | `UPLOAD_PART_MISMATCH` | Mesma parte reenviada com conteúdo diferente | ambos |
| 409 | `UPLOAD_NOT_FOUND` | `retry` de item de lote sem sessão de upload para retomar | ambos |
| 409 | `ARTIFACT_QUARANTINED` | Export reprovado na auditoria. O download é bloqueado | ambos |
| 409 | `ARTIFACT_HASH_MISMATCH` | O artefato no storage não confere com o hash registrado | ambos |
| 409 | `TEXT_PLAN_MISSING` | Exportação TXT de um run sem plano textual | standalone |
| 410 | `UPLOAD_EXPIRED` | Sessão de upload expirada | standalone. O gateway responde `409` com o mesmo código |

### Envio por partes e checksums

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 413 | `FILE_TOO_LARGE` | Parte maior que o tamanho de parte negociado | standalone. O gateway responde `400` |
| 422 | `UPLOAD_CHECKSUM_MISMATCH` | SHA-256 de uma parte ou do documento não confere, ou `sha256` com formato inválido | standalone |
| 422 | `VALIDATION_ERROR` | Mesmo caso acima. O código específico vai em `details.code = UPLOAD_CHECKSUM_MISMATCH` | gateway |
| 415 | `CODEC_UNAVAILABLE` | Imagem cujo formato não tem codec instalado na máquina | standalone |

### Serviços e indisponibilidade

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 500 | `INTERNAL` | Falha inesperada. A ocorrência é registrada | ambos |
| 503 | `BATCH_DISABLED` | Envio em lote desativado | standalone. O gateway responde `400 INVALID_OPTION` com `reason: batch_disabled` |
| 503 | `CONVERTER_UNAVAILABLE` | Motor Python indisponível | gateway |
| 503 | `STORAGE_UNAVAILABLE` | Falha de disco ou S3 ao gravar uma parte ou ler um objeto | gateway |

### Códigos fora do HTTP

Estes códigos aparecem dentro de outros payloads, não como status de erro.

- **`error.code` de uma conversão** (campo do run). O `retryable` é `false` para
  `CANCELLED`, `INVALID_FILE` e `ENCRYPTED_PDF`. Códigos emitidos pelo pipeline:
  `INVALID_FILE`, `ENCRYPTED_PDF`, `PDF_UNREADABLE`, `DECODE_FAILED`, `NO_CONTENT`,
  `NO_TEXT_RECOVERED`, `OCR_UNAVAILABLE`, `TIME_LIMIT`, `CANCELLED`,
  `INTERNAL_ERROR`, `XLSX_MISSING`, `STORAGE_READ_FAILED`, `FRAME_DECODE_FAILED`,
  `FRAME_LIMIT` e `PIXEL_LIMIT` (imagens).
- **Exportação TXT/CSV** (standalone, 400): `NO_TABLES_FOUND`, `TABLE_NOT_FOUND`,
  `EXPORT_PLAN_MISSING`, `INVALID_OPTION` e `UNSUPPORTED_FORMAT`. `CSV_FORMULA_NEUTRALIZED`
  é um aviso, não um erro.
- **`QUEUE_DELIVERY_FAILED`**: a tarefa não chegou ao motor. Aparece no evento de
  outbox e no status da conversão (gateway).
- **Eventos `error` do assistente**: `MODEL_DISABLED`, `MODEL_UNAVAILABLE`,
  `MODEL_TIMEOUT`, `MODEL_ERROR`, `EMBEDDING_UNAVAILABLE`, `NOT_FOUND`.
- **Jobs de indexação** (`/knowledge/index-jobs/:id`): `INDEX_FAILED` e
  `UNSUPPORTED_FILE`.

### Idempotência

- `POST /conversions`, mesma `Idempotency-Key`:
  - standalone: mesmo arquivo e mesmas opções devolvem a conversão existente.
    Arquivo ou opções diferentes retornam `409 IDEMPOTENCY_KEY_REUSED`.
  - gateway: payload diferente retorna `409 IDEMPOTENT_REPLAY` com
    `reason: payload_mismatch`. Mesmo payload devolve a conversão existente.
- Lotes, `client_item_id` repetido: mesmos dados devolvem o mesmo item e o mesmo
  `upload_id` com `replayed: true`. Dados ou opções diferentes retornam `409 IDEMPOTENT_REPLAY`.

### Códigos declarados e nunca emitidos

Estes códigos existem em `apps/api/src/common/errors/error-codes.ts` ou nos
contratos, mas nenhum caminho de código do projeto os produz. Não os use como
base de tratamento de erro:

| code | Situação |
| --- | --- |
| `RATE_LIMITED` | Mapeado no filtro de exceções a partir de HTTP 429, mas nenhum caminho gera 429. Não há limitação de taxa |
| `RATE_LIMIT_PRESSURE` | Declarado, sem produtor |
| `QUEUE_FULL` | Declarado, sem produtor. A fila é um pool com trabalho durável e não rejeita por tamanho |
| `INSUFFICIENT_STORAGE` | Declarado, sem produtor |
| `EMAIL_ALREADY_EXISTS` | Declarado. Não há cadastro |
| `INVALID_CREDENTIALS` | Declarado. Existe um construtor `ApiError.invalidCredentials()` sem chamadas. Não há login |
| `UNSUPPORTED_TARGET_FORMAT` | Declarado no gateway e no contrato `packages/contracts`. Formato desconhecido é `INVALID_OPTION` (gateway) ou `UNSUPPORTED_FORMAT` (standalone) |
| `BATCH_ITEM_MISMATCH` | Declarado no gateway, sem produtor no gateway nem no standalone. O cliente MCP (`apps/mcp`) trata o código |

## Diferenças entre os modos

| Assunto | standalone (FastAPI) | full (NestJS) |
| --- | --- | --- |
| Formato de alvo desconhecido | `400 UNSUPPORTED_FORMAT` | `400 INVALID_OPTION` (validação do contrato) |
| Imagem com XLSX ou CSV | `400 UNSUPPORTED_TARGET` | `400 INCOMPATIBLE_OUTPUT_FORMAT` |
| Sessão de upload inexistente | `404 UPLOAD_NOT_FOUND` | `404 NOT_FOUND` |
| Sessão de upload expirada | `410 UPLOAD_EXPIRED` | `409 UPLOAD_EXPIRED` |
| Parte acima do tamanho | `413 FILE_TOO_LARGE` | `400 FILE_TOO_LARGE` |
| Checksum divergente | `422 UPLOAD_CHECKSUM_MISMATCH` | `422 VALIDATION_ERROR` com `details.code` |
| Conversão em andamento no `retry` | `409 ALREADY_ACTIVE` | `409 CONVERSION_BUSY` |
| `Idempotency-Key` com payload diferente | `409 IDEMPOTENCY_KEY_REUSED` | `409 IDEMPOTENT_REPLAY` |
| Lotes desativados | `503 BATCH_DISABLED` | `400 INVALID_OPTION` (`reason: batch_disabled`) |
| Motor indisponível | não se aplica (mesmo processo) | `503 CONVERTER_UNAVAILABLE` |
| Cancelar conversão | `202` | `200` |
| `POST /knowledge/search` | disponível | `404 NOT_FOUND`, não encaminhado |
| `GET /health/ready` | disponível | `404` |
| Teto de páginas (`UPLOAD_MAX_PAGES`) | não aplicado | `400 TOO_MANY_PAGES` quando definido |

No gateway, as rotas de paridade encaminham ao motor com `x-internal-token` e o
`owner_id` já autenticado. O gateway valida o usuário antes de encaminhar. O
motor aplica o controle de acesso pelo `owner_id` recebido no canal interno.
Rotas de paridade: `GET /capabilities`, `GET /models/status`,
`GET|POST /conversions/:id/exports`, `GET .../exports/:exportId/download`,
`GET /conversions/:id/text`, `/knowledge/*` (exceto `search`), `/chat/*`
(incluindo o stream SSE de mensagens).

## Lotes e envio por partes

Um lote (`batch`) é um contêiner durável para um número sem teto de documentos.
Cada item vira exatamente uma conversão. O lote nunca funde arquivos em uma
única planilha. As rotas abaixo existem nos dois modos.

### Lotes

| Método | Rota | Corpo | Resposta |
| --- | --- | --- | --- |
| `POST` | `/batches` | `{client_batch_key}` | 201 `BatchSummary`. Repetir a chave devolve o mesmo lote |
| `GET` | `/batches/:id` | — | `BatchSummary` |
| `POST` | `/batches/:id/items` | `{client_item_id, filename, size_bytes, options}` | 201 `{item, upload_id, part_size, next_part, replayed, expires_at}` |
| `GET` | `/batches/:id/items?cursor&limit&status` | — | `{items, next_cursor}` |
| `POST` | `/batches/:id/seal` | — | `{batch_id, status, sealed_at, message}` |
| `POST` | `/batches/:id/cancel` | — | `BatchSummary` |
| `POST` | `/batches/:id/items/:itemId/cancel` | — | `BatchItem` |
| `POST` | `/batches/:id/items/:itemId/retry` | `{scope: "upload"\|"conversion"}` | `{item, ...}` |
| `GET` | `/batches/:id/events` | `Last-Event-ID`, `?access_token=` | SSE agregado, com sequência por lote |

Regras:

- `client_batch_key` é único por dono. Repetir devolve o mesmo lote.
- `client_item_id` é idempotente, como descrito em [Idempotência](#idempotência).
- `limit` tem padrão 50 e máximo 200. É paginação, não um teto de lote.
- Selar com uploads em `pending_upload` ou `uploading` retorna `409 BATCH_HAS_PENDING_UPLOADS`
  com a contagem. `upload_failed` é definitivo e não bloqueia a selagem.
- Um lote selado vazio conclui com `completed` e zero itens.

### Upload por partes

| Método | Rota | Corpo | Resposta |
| --- | --- | --- | --- |
| `PUT` | `/uploads/:id/parts/:partNumber` | bytes crus. Cabeçalho opcional `x-part-sha256` | 200 `UploadPartReceipt` |
| `GET` | `/uploads/:id?cursor&limit` | — | `UploadSessionDetail` |
| `GET` | `/uploads/:id/parts?cursor&limit` | — | idem |
| `POST` | `/uploads/:id/complete` | `{sha256, total_parts}` | 202 `{upload_id, item_id, conversion_id, status, message, replayed}` |
| `DELETE` | `/uploads/:id` | — | `{upload_id, state, deleted_parts}` |

- `part_size` é decidido pelo servidor (8 MiB por padrão, em `UPLOAD_PART_BYTES`)
  e devolvido no registro do item. É um parâmetro de transporte e não limita o
  tamanho total.
- As partes são numeradas a partir de **0**. Reenviar a mesma parte é uma
  operação sem efeito. Reenviar conteúdo diferente retorna `409 UPLOAD_PART_MISMATCH`.
- `complete` confere contiguidade, tamanho total e SHA-256 do documento antes de
  criar a conversão. Repetir o `complete` devolve os mesmos IDs e **não** cria uma
  segunda conversão.
- SHA-256 de parte divergente: `422 UPLOAD_CHECKSUM_MISMATCH`. Ver a tabela de
  checksums para o formato no gateway. A parte não é confirmada.

Escrita em disco por endpoint (detalhes em [STORAGE.md](STORAGE.md)):

- `PUT /uploads/:id/parts/:n`: grava a parte numa área de staging do storage e
  confirma a parte no banco.
- `POST /uploads/:id/complete`: monta o documento a partir das partes, valida o
  SHA-256 e grava o PDF original no storage antes de criar a conversão.
- `POST /conversions` (envio único): grava o PDF original no storage antes de
  criar a conversão.
- `POST /conversions/:id/exports`: gera o artefato TXT ou CSV e grava no storage,
  com hash e `publication_status`.

### Resultado compacto

| Método | Rota | Observação |
| --- | --- | --- |
| `GET` | `/conversions/:id/result-summary` | O tamanho da resposta não depende do número de células: traz contagens, não as células |
| `GET` | `/conversions/:id/sheets?cursor&limit&q` | Metadados do seletor de abas (`limit` máximo 200) |
| `GET` | `/conversions/:id/sheets/:sheetId/pages?cursor&limit` | Páginas de origem da aba, paginadas |

`result-summary` devolve `counts`, `available_formats`, `default_sheet`,
`warning_counts`, `partial`, `pages_failed`, `pages_total` e os links. A lista de
páginas de origem não vem embutida: use a rota paginada.

## Opções (`ConversionOptionsV2`)

```json
{
  "schema_version": 2,
  "target_formats": ["xlsx", "txt", "csv"],
  "ocr": { "enabled": true, "languages": ["por", "eng"], "dpi": null, "strategy": "auto" },
  "text": { "newline": "lf", "page_separator": "form_feed", "include_table_text": true },
  "csv": { "profile": "interoperable", "delimiter": null, "formula_policy": "safe", "table_ids": null, "value_mode": "original" }
}
```

- Imagem com `xlsx` ou `csv` é rejeitada, como descrito em [Códigos de erro](#códigos-de-erro).
  Sem `target_formats`, a imagem usa `["txt"]`.
- As opções normalizadas entram no hash de idempotência, em forma canônica.
- Formatos de saída válidos: `xlsx`, `txt`, `csv`.
- Aceitas também as chaves planas antigas (`ocr`, `text` e `csv` no nível raiz),
  só para compatibilidade com a rota de envio único.
