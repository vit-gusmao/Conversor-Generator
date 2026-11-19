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
- Eventos em tempo real: SSE. Para retomar, envie `Last-Event-ID`. Nas rotas de
  conversão e de lote, o cursor também pode vir em `?last_event_id=` (o
  gateway aceita essa forma; o motor standalone também).
- Nome de arquivo nos downloads: `Content-Disposition` com `filename` ASCII de
  reserva e `filename*=UTF-8''...` com o nome exato (RFC 6266/5987).

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
| GET | `/conversions/:id/events` | SSE de progresso. Aceita `Last-Event-ID`, `?last_event_id=` e `?access_token=`. `?run_id=` acompanha só uma execução nos dois modos (ver [Eventos de conversão](#eventos-de-conversão)) |
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

No modo desktop (`DESKTOP_HOST_ENABLED=true`), `GET /health` (na raiz, sem
`/api/v1`) é a única rota sem o cabeçalho `x-desktop-host`; as demais respondem
`403 DESKTOP_HOST_FORBIDDEN`. Por isso, `/api/v1/health` não é isento.

### Rotas internas (`/internal/v1`)

Rotas do motor chamadas pelo gateway, com `x-internal-token`. Não são contrato
para clientes do navegador. Incluem, entre outras, `GET /internal/v1/health`,
`GET /internal/v1/automation/status`, `GET /internal/v1/runtime/activity` (atividade
do runtime) e `POST /internal/v1/automation/pause`.
Token ausente ou inválido responde `401 UNAUTHORIZED_INTERNAL`.

### Fontes, importações e agendamentos

Rotas do upgrade de importação por caminho. Existem nos dois modos: no `full`
o gateway autentica a sessão e encaminha ao motor pelo canal interno, sempre
com o dono verificado (o cliente nunca envia `owner_id`).

**Raízes concedidas.** A concessão é uma ação administrativa local, não uma
rota pública: no desktop ela usa a credencial do host; no site local, a rota
interna `POST /internal/v1/source-roots` com `x-internal-token`.

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/source-roots` | Raízes visíveis ao dono (`id`, `label`, `runtime_id`, `runtime_available`, `display_path`). O caminho absoluto aparece só para o próprio dono |
| GET | `/sources` | Fontes do dono |
| POST | `/sources` | Cria fonte: `{root_id, relative_path, kind: file\|directory, name, recursive?, include_patterns?, exclude_patterns?}`. `400 UNKNOWN_FIELD` para campos desconhecidos (inclusive `owner_id`), `400 INVALID_SOURCE` para caminho inválido/travessia, `400 SOURCE_UNAVAILABLE` quando o caminho não existe, `404 ROOT_NOT_FOUND` para raiz alheia |
| GET | `/sources/{id}` | Fonte do dono (`404` para alheia) |
| PATCH | `/sources/{id}` | Edição otimista: exige `version`; conflito retorna `409 VERSION_CONFLICT`. `relative_path`/`kind` passam pela mesma política da criação. No gateway (`full`), `PUT` é aceito com o mesmo corpo e as mesmas regras; no standalone, `PUT` existe só nas rotas internas |
| DELETE | `/sources/{id}` | Arquiva a fonte (histórico preservado) |
| POST | `/sources/{id}/validate` | Amostra de até 100 arquivos, contagem e bytes estimados. Não inicia conversão. Fonte inacessível responde `ok:false` com código (`SOURCE_NOT_FOUND`, `ROOT_REVOKED`) |
| POST | `/sources/{id}/imports` | **202** com cabeçalho `Idempotency-Key` obrigatório. Mesma chave + mesmo corpo devolve a mesma importação; corpo diferente → `409 IDEMPOTENCY_KEY_REUSED`. Importação ativa da fonte → `409 SOURCE_IMPORT_BUSY` |
| GET | `/imports/{id}` | Progresso e contadores (`imported`, `skipped`, `failed`) |
| GET | `/imports/{id}/items` | Itens por arquivo, com `conversion_id` e erro individual |
| POST | `/imports/{id}/cancel` | Pedido durável; itens já publicados permanecem no histórico |
| GET | `/schedules` | Agendas do dono |
| POST | `/schedules` | Regra diária (`{kind:"daily", local_time:"HH:MM"}`) ou intervalo (`{kind:"interval", every_seconds, anchor_at?}`), `timezone` IANA, políticas `misfire_policy`/`overlap_policy`. `400 INVALID_SCHEDULE_RULE` para regra/intervalo inválido e `INVALID_TIMEZONE` para fuso desconhecido |
| POST | `/schedules/preview` | Próximas cinco ocorrências, sem executar (mesma função do disparo real) |
| GET/PATCH/PUT/DELETE | `/schedules/{id}` | PATCH e PUT aceitos nos dois modos e com o mesmo corpo: exigem `revision` (conflito → `409 REVISION_CONFLICT`); DELETE arquiva |
| POST | `/schedules/{id}/pause` \| `/resume` \| `/run-now` | Transições idempotentes. `run-now` exige `Idempotency-Key`, não desloca o calendário e responde 202 com `executor_active` (ver [Agendamentos](SOURCES_AND_AUTOMATION.md#4-agendamentos)) |
| GET | `/schedules/{id}/runs` | Histórico de ocorrências |
| GET | `/schedule-runs/{id}` | Estado, fase, importação, indexação e erro |
| GET | `/schedule-runs/{id}/events` | Eventos. Com `Accept: text/event-stream` devolve SSE, retomado por `Last-Event-ID` ou `?after=` (o cabeçalho tem precedência); caso contrário devolve `{items}` para polling. O SSE envia `event: done` quando a ocorrência termina sem eventos novos |
| POST | `/schedule-runs/{id}/cancel` | Cancelamento durável e idempotente |

### Eventos de conversão

`GET /conversions/:id/events` (SSE). Cada evento traz `id` (sequência) e `event`
(tipo). O `data` é JSON com `status`; no gateway ele traz também `run_id`, no
standalone não (o filtro por execução é feito no servidor nos dois modos).

Os dois modos seguem o mesmo contrato de `run_id`:

- Sem `run_id`, o stream acompanha a conversão como um todo e termina no evento
  terminal da execução atual. Um `retry` reutiliza o id da conversão; a falha de
  uma execução antiga não fecha o stream.
- Com `?run_id=`, o stream repassa só os eventos daquela execução e termina no
  evento terminal dela. Uma execução anterior que falhou não fecha o stream de
  uma execução nova.
- `run_id` inexistente para a conversão responde `404 NOT_FOUND`; formato
  inválido (fora de `^[A-Za-z0-9_-]{1,64}$`) responde `400 INVALID_OPTION` no
  standalone e `400` no gateway. Essas respostas saem antes do início do stream.
- O standalone guarda a execução de cada evento pelo evento de enfileiramento
  da execução (`payload.attempt`), por isso não precisa de coluna `run_id` nos
  eventos.

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

Códigos adicionados pelo upgrade de fontes/planilhas/agendador:
`UNKNOWN_FIELD` (400), `INVALID_PAYLOAD` (400), `INVALID_SOURCE` (400),
`SOURCE_UNAVAILABLE` (400), `ROOT_NOT_FOUND` (404), `ROOT_REVOKED` (409),
`SOURCE_DISABLED` (409), `FEATURE_DISABLED` (409, só em `POST /sources/{id}/imports`
com `SOURCE_IMPORTS_ENABLED=false`; `run-now` não o devolve), `VERSION_CONFLICT` (409),
`IDEMPOTENCY_KEY_REUSED` (409), `SOURCE_IMPORT_BUSY` (409),
`INVALID_SCHEDULE_RULE` (400), `INVALID_TIMEZONE` (400),
`REVISION_CONFLICT` (409), `SCHEDULE_NOT_FOUND` (404), `RUN_NOT_FOUND` (404),
`UNSUPPORTED_SPREADSHEET_FORMAT` (400), `NOT_SPREADSHEET` (400),
`SPREADSHEET_CORRUPT` (400), `SPREADSHEET_BUDGET_EXCEEDED` (400),
`SPREADSHEET_SHEET_NOT_FOUND` (400), `CSV_ENCODING_REQUIRED` (400),
`CSV_DIALECT_REQUIRED` (400), `SOURCE_CHANGED` / `SOURCE_BUSY` /
`SOURCE_MISSING` (erro por item), `FILE_TOO_LARGE` (400 por item).

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
| 400 | `CSV_ENCODING_REQUIRED` | CSV com bytes que não decodificam como UTF-8 (com ou sem BOM) e sem `csv_encoding` declarado | standalone, no `complete` do envio por partes |
| 400 | `CSV_DIALECT_REQUIRED` | CSV com delimitador ambíguo (ou sem delimitador) sem `csv_delimiter` declarado | standalone, no `complete` |
| 400 | `CSV_PARSE_FAILED` | CSV que não pôde ser lido com o dialeto escolhido | standalone, no `complete` |
| 400 | `NOT_SPREADSHEET` | Arquivo com extensão de planilha cujo conteúdo não é XLSX/CSV (por exemplo, ZIP renomeado) | standalone, no `complete` |
| 400 | `SPREADSHEET_CORRUPT` | Pacote XLSX corrompido ou incompleto | standalone, no `complete` |
| 400 | `UNSUPPORTED_SPREADSHEET_FORMAT` | `.xls`, `.xlsm`, `.xlsb` ou `.ods`, recusados com mensagem explícita | standalone, no `complete` |
| 400 | `SPREADSHEET_BUDGET_EXCEEDED` | Orçamento de expansão, células ou tamanho estourado (nunca há truncamento) | standalone, no `complete` |
| 400 | `SPREADSHEET_SHEET_NOT_FOUND` | Aba pedida em `spreadsheet.sheet_names` que não existe | standalone, no `complete` |

Os códigos de planilha acima vêm da sondagem do motor no `complete` do envio
por partes e são devolvidos como estão, com `400`, e o item vira `upload_failed`.
No gateway (`full`), `complete` admite XLSX e CSV declarados pelo nome, sem
sondagem de planilha; a validação de conteúdo fica com o motor na execução da
conversão. Esses códigos específicos não são devolvidos pelo `complete` do
gateway.
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
| 401 | `UNAUTHORIZED_INTERNAL` | `x-internal-token` ausente ou inválido nas rotas `/internal/v1` do motor, e nos callbacks internos do motor para o gateway (guard `InternalTokenGuard`) | standalone, motor e gateway (callbacks) |
| 403 | `FORBIDDEN` | Recurso de outro dono | ambos |
| 403 | `DESKTOP_HOST_FORBIDDEN` | Requisição sem o cabeçalho `x-desktop-host` correto, com `Host` ou `Origin` de outra origem. Só existe com `DESKTOP_HOST_ENABLED=true` (aplicativo desktop). `GET /health` é a única exceção | standalone (desktop) |
| 404 | `NOT_FOUND` | Recurso inexistente, rota inexistente ou rota que o gateway não encaminha | ambos |
| 404 | `UPLOAD_NOT_FOUND` | Sessão de upload inexistente em `GET`, `PUT` ou `complete` | standalone. O gateway responde `NOT_FOUND` |

### Estado e concorrência

| HTTP | code | Quando | Modo |
| --- | --- | --- | --- |
| 409 | `ALREADY_TERMINAL` | `cancel` numa conversão já terminal. O `retry` de conversão terminal é permitido e cria nova execução | ambos |
| 409 | `ALREADY_ACTIVE` | `retry` numa conversão ainda em andamento | standalone. O gateway responde `CONVERSION_BUSY` |
| 409 | `CONVERSION_BUSY` | Operação que exige conversão parada, com a conversão em andamento (`retry` no gateway; repetir item de lote). No gateway, também numa corrida de `retry` | ambos |
| 409 | `ALREADY_CONVERTED` | Repetir o **envio** (`scope: "upload"`) de um item que já virou conversão. Use `scope: "conversion"` | standalone. O gateway responde `UPLOAD_STATE_CONFLICT` com `reason: upload_not_resumable` |
| 409 | `NOT_READY` | Resultado, XLSX ou prévia ainda não disponíveis, ou repetir item de lote sem conversão | ambos |
| 409 | `IDEMPOTENT_REPLAY` | Ver [Idempotência](#idempotência) | ambos |
| 409 | `IDEMPOTENCY_KEY_REUSED` | `Idempotency-Key` repetida com outro arquivo ou outras opções. Nunca devolve a conversão anterior | standalone |
| 409 | `BATCH_NOT_OPEN` | Lote selado, que não aceita mais itens | ambos |
| 409 | `BATCH_CANCELLED` | Lote cancelado | ambos |
| 409 | `BATCH_HAS_PENDING_UPLOADS` | Selar um lote com uploads em `pending_upload` ou `uploading`. Com a contagem em `details`. `upload_failed` não bloqueia | ambos |
| 409 | `UPLOAD_STATE_CONFLICT` | Sessão já concluída ou abortada, ou ação inválida para o estado | ambos |
| 409 | `UPLOAD_INCOMPLETE` | `complete` com partes faltando, total de partes diferente ou bytes divergentes | ambos |
| 409 | `UPLOAD_EXPIRED` | Sessão de upload expirada no `PUT` de parte e no `complete` (gateway). O standalone usa o mesmo código com `410`, ver abaixo | gateway |
| 409 | `UPLOAD_PART_MISMATCH` | Mesma parte reenviada com conteúdo diferente | ambos |
| 409 | `UPLOAD_NOT_FOUND` | `retry` de item de lote sem sessão de upload para retomar. (Em `GET`/`PUT`/`complete` de sessão inexistente, ver a tabela de autenticação, linha 404) | ambos |
| 409 | `ARTIFACT_QUARANTINED` | Export reprovado na auditoria. O download é bloqueado | ambos |
| 409 | `ARTIFACT_HASH_MISMATCH` | O artefato no storage não confere com o hash registrado | ambos |
| 409 | `TEXT_PLAN_MISSING` | Exportação TXT de um run sem plano textual | standalone |
| 410 | `UPLOAD_EXPIRED` | Sessão de upload expirada no `PUT` de parte e no `complete`. Nenhuma conversão é criada a partir de uma sessão expirada | standalone. O gateway responde `409` com o mesmo código, no `PUT` e no `complete` (só o status HTTP difere) |

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
- **Importações por caminho** (`error.code` da importação, e `error` do item):
  `IMPORT_INCOMPLETE` (ainda há itens em andamento; retomável),
  `IMPORT_GATEWAY_UNAVAILABLE` (falha transitória ao submeter; retomável),
  `IMPORT_FAILED`, `IMPORT_INTERNAL_ERROR`, `IMPORT_INVALID`. Erros de item:
  `SOURCE_CHANGED`, `SOURCE_BUSY`, `SOURCE_MISSING`, `FILE_TOO_LARGE`,
  `SNAPSHOT_MISSING` e `CONVERSION_FAILED`.
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
| Repetir envio de item já convertido (`scope: upload`) | `409 ALREADY_CONVERTED` | `409 UPLOAD_STATE_CONFLICT` (`reason: upload_not_resumable`) |
| Sessão de upload expirada no `complete` | `410 UPLOAD_EXPIRED` (sem criar conversão) | `409 UPLOAD_EXPIRED` (sem criar conversão) |
| Desktop: requisição sem host válido | `403 DESKTOP_HOST_FORBIDDEN` (só com `DESKTOP_HOST_ENABLED`) | não se aplica |
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
- `upload_failed` é estado terminal e conta como falha no lote. No gateway, entra
  em `counts.failed`. No motor standalone, aparece como chave própria
  `upload_failed` em `counts`, mas também conta como problema para o status do
  lote.
- Um lote selado vazio conclui com `completed` e zero itens. Com problemas, o
  status é `completed_with_errors`.
- Repetir item (`POST /batches/:id/items/:itemId/retry`, body `{scope}`):
  - `scope: "upload"` retoma o envio na mesma sessão. No gateway só vale para
    itens em `pending_upload`, `uploading` ou `upload_failed`; outro estado
    responde `409 UPLOAD_STATE_CONFLICT`. Item que já virou conversão não é
    reenviado (`409`, ver [Estado e concorrência](#estado-e-concorrência)).
    O standalone aplica a mesma regra de estado: outro estado responde
    `409 UPLOAD_STATE_CONFLICT` com `details.reason: upload_not_resumable`. Item
    com conversão responde `409 ALREADY_CONVERTED` (o gateway responde
    `UPLOAD_STATE_CONFLICT` nesse caso). Um item abortado (`upload_failed`)
    passa pela regra; como o aborto remove a sessão do item, a repetição então
    responde `409 UPLOAD_NOT_FOUND` nos dois modos.
  - `scope: "conversion"` cria nova execução da conversão do item. Vale para
    item já finalizado. Item em andamento responde `409 CONVERSION_BUSY`; item
    sem conversão responde `409 NOT_READY`.
  - Outro `scope` responde `400 INVALID_OPTION`.

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
- O caminho por partes não tem teto de tamanho do produto. `UPLOAD_MAX_BYTES`
  vale só para `POST /conversions` (envio único). No gateway, `UPLOAD_MAX_FILE_BYTES`
  é um teto opcional, desligado por padrão, aplicado ao registrar o item.
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

### Opções v3 (`ConversionOptionsV3`) — entrada de planilha

Um payload vira v3 quando `kind` é planilha **ou** existe a seção
`spreadsheet`; `schema_version: 3` sozinho não promove um PDF (o formato v2
continua byte a byte igual, preservando o hash de idempotência).

```json
{
  "schema_version": 3,
  "target_formats": ["xlsx", "txt", "csv"],
  "spreadsheet": {
    "sheet_names": ["Faturas"],
    "header_row": true,
    "csv_encoding": "utf-8-sig",
    "csv_delimiter": ";",
    "formula_mode": "cached_or_text",
    "reader_version": "spreadsheet-reader-v1"
  }
}
```

- `sheet_names`: `null` lê todas as abas (ocultas incluídas, com aviso);
  nomes inexistentes falham com `SPREADSHEET_SHEET_NOT_FOUND`.
- `header_row`: `true`/`false` explícitos ou `null` (decisão determinística
  registrada na auditoria da aba).
- `csv_encoding`/`csv_delimiter`: obrigatórios para arquivos não-UTF-8 ou com
  dialeto ambíguo (`CSV_ENCODING_REQUIRED`, `CSV_DIALECT_REQUIRED`).
- `formula_mode` aceita apenas `cached_or_text`: fórmulas sem valor em cache
  entram como texto, com aviso `FORMULA_WITHOUT_CACHE`.
- `reader_version` é **estampado pelo servidor** e participa do fingerprint;
  enviá-lo em entrada de cliente é `INVALID_OPTION`.
- XLSX é reconhecido pelo conteúdo (OOXML); ZIP renomeado é `NOT_SPREADSHEET`.
  Orçamentos de expansão/células/grade produzem `SPREADSHEET_BUDGET_EXCEEDED`
  em vez de truncar. `.xls`, `.xlsm`, `.xlsb` e `.ods` são recusados nesta
  versão com mensagem explícita.

