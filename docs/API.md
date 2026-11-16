# API — contrato público

Base URL: `/api/v1` (NestJS em modo `full`, FastAPI em modo `standalone`).
Todos os payloads são JSON exceto upload e download.
Autenticação: `Authorization: Bearer <jwt>`. Em `AUTH_MODE=optional` (padrão de
desenvolvimento) requisições anônimas recebem uma sessão de dispositivo e só
enxergam as próprias conversões.

## Convenções

- Identificadores: UUID v4.
- Datas: ISO-8601 UTC com sufixo `Z`.
- Erros: `{ "error": { "code": "...", "message": "...", "details": {...} } }`
  com HTTP 4xx/5xx apropriado.
- Paginação: `?page=1&page_size=20` (máx. 100) → `{ items, total, page, page_size }`.
- Idempotência: `Idempotency-Key` no upload.
- SSE: `GET /conversions/:id/events`, retomada com `Last-Event-ID`.

## Endpoints

### Sessão local

Não existe cadastro, login nem senha. Cada navegador recebe uma identidade
própria (`x-device-id` + cookie HttpOnly `pdf2x_device_id`); o token JWT é
emitido para essa identidade e todas as conversões, artefatos e conversas ficam
escopados a ela.

| Método | Endpoint | Descrição |
| --- | --- | --- |
| POST | `/auth/anonymous` | Cria/retoma a sessão local do navegador → `AuthSession` |
| GET | `/auth/me` | Identidade local atual → `User` |

### Conversões

| Método | Endpoint | Descrição |
| --- | --- | --- |
| POST | `/conversions` | `multipart/form-data` campo `file`. Retorna **202** `{conversion_id, status:"queued", message, links}` |
| GET | `/conversions` | Histórico. Filtros: `status`, `q`, `page`, `page_size` |
| GET | `/conversions/:id` | Status detalhado + stats + validação |
| GET | `/conversions/:id/events` | SSE de progresso |
| GET | `/conversions/:id/result` | Metadados do resultado (`ResultSummary`) |
| GET | `/conversions/:id/preview` | `?sheet=&offset=&limit=` → `Preview` |
| GET | `/conversions/:id/pages` | Páginas processadas: dimensões, classificação, `text_source`, cobertura e nº de regiões |
| GET | `/conversions/:id/tables` | Tabelas reconstruídas: cabeçalhos, linhas, estratégia, mesclagens e continuidade |
| GET | `/conversions/:id/cells/:cellId` | Origem de uma célula, endereçada como `Aba!C2` ou `C2` (rastreabilidade do plano) |
| GET | `/conversions/:id/download` | Baixa o `.xlsx` |
| GET | `/conversions/:id/exports` | Lista artefatos TXT/CSV/ZIP com hash, tamanho, avisos e `publication_status` |
| POST | `/conversions/:id/exports` | Cria artefato a partir do IR armazenado: `{format:"txt"|"csv", options}` |
| GET | `/conversions/:id/exports/:exportId/download` | Baixa o artefato aprovado; quarentena retorna 409 |
| GET | `/conversions/:id/text` | Texto paginado: `?page=&offset=&limit=` |
| POST | `/conversions/:id/retry` | Cria novo `run`; retorna 202 |
| POST | `/conversions/:id/cancel` | Solicita cancelamento |
| DELETE | `/conversions/:id` | Exclusão lógica + remoção de artefatos |

### Capacidades e modelo

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/capabilities` | Entradas e codecs instalados, saídas (xlsx/txt/csv), limites e estado do OCR |
| GET | `/models/status` | Modelo configurado, disponibilidade, digest, contexto e concorrência (sem segredos) |

### Conhecimento (RAG)

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/knowledge/collections` | Coleções do proprietário com contagem de chunks |
| POST | `/knowledge/collections` | `{name, description?, scope:"documents"|"studies"}` |
| DELETE | `/knowledge/collections/:id` | Remove a coleção e seus chunks |
| POST | `/knowledge/index-jobs` | `{collection_id?, conversion_ids[], source_paths[]}` → 202 job |
| GET | `/knowledge/index-jobs/:id` | Estado do job e estatísticas |
| POST | `/knowledge/search` | `{query, collection_id?, top_k?}` → trechos com citação e score |

### Assistente

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/chat/conversations` | Conversas do proprietário |
| POST | `/chat/conversations` | `{title?, collection_id?, document_id?, run_id?}` |
| GET | `/chat/conversations/:id` | Conversa com histórico e citações |
| DELETE | `/chat/conversations/:id` | Exclusão lógica |
| POST | `/chat/conversations/:id/messages` | SSE com eventos `status`, `delta`, `citation`, `done`, `error` |

Upload aceita `options` (JSON) junto do arquivo:

```json
{
  "schema_version": 2,
  "target_formats": ["xlsx", "txt", "csv"],
  "ocr": { "enabled": true, "dpi": 300 },
  "text": { "newline": "lf", "page_separator": "form_feed" },
  "csv": { "profile": "interoperable", "formula_policy": "safe" }
}
```

Opções desconhecidas são rejeitadas com `INVALID_OPTION`. Imagens aceitam apenas
`target_formats: ["txt"]`; outro alvo retorna `UNSUPPORTED_TARGET`.

### Auditoria

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/conversions/:id/audit` | `AuditSummary` |
| GET | `/conversions/:id/audit/events` | Eventos. Filtros `stage`, `event_type`, `severity`, `page`, `since_sequence`, `limit` |
| GET | `/conversions/:id/audit/pages/:page` | `PageAudit` |
| GET | `/conversions/:id/audit/cells` | Proveniência. Filtros `sheet`, `cell`, `limit`. `transformations` e `value_matches_source` são calculados a partir do par original/normalizado |
| GET | `/conversions/:id/audit/issues` | `{ items: AuditIssue[], total }` |
| GET | `/conversions/:id/audit/export` | `?format=json|txt` download |

### Sistema

| Método | Endpoint | Descrição |
| --- | --- | --- |
| GET | `/settings` | `PlatformSettings` |
| GET | `/health` | `{status, services:{db, queue, storage, converter}}` |

## Regras de estado

```
received → validating → queued → inspecting → extracting → structuring
        → normalizing → reconciling → generating → auditing
        → completed | completed_with_warnings | failed | cancelled
```

- Estados terminais nunca voltam a ativos; `retry` cria um novo `conversion_run`.
- `progress` em `[0,1]`; `progress_indeterminate=true` quando a etapa não tem
  estimativa confiável.
- Nenhuma etapa publica dados finais sem registrar evento de auditoria antes.

## Códigos de erro

| HTTP | code | Quando |
| --- | --- | --- |
| 400 | `INVALID_FILE` | não é PDF ou está corrompido |
| 400 | `FILE_TOO_LARGE` | acima de `UPLOAD_MAX_BYTES` |
| 400 | `TOO_MANY_PAGES` | acima de `UPLOAD_MAX_PAGES` |
| 401 | `UNAUTHENTICATED` | token ausente/inválido em modo `required` |
| 403 | `FORBIDDEN` | conversão de outro usuário |
| 404 | `NOT_FOUND` | recurso inexistente |
| 409 | `ALREADY_TERMINAL` | retry/cancel inválido |
| 409 | `IDEMPOTENT_REPLAY` | mesma `Idempotency-Key` com o **mesmo** arquivo (replay) |
| 409 | `IDEMPOTENCY_KEY_REUSED` | mesma `Idempotency-Key` com **outro** arquivo — recusado, nunca devolve a conversão anterior |
| 422 | `VALIDATION_ERROR` | payload inválido |
| 429 | `RATE_LIMITED` | limite de requisições |
| 500 | `INTERNAL` | falha inesperada (auditada) |
| 503 | `CONVERTER_UNAVAILABLE` | motor Python indisponível |

## Fora do escopo atual (lacunas declaradas)

O contrato do plano prevê dois itens que **não** estão implementados. Ficam
registrados aqui para que nenhum cliente assuma que existem:

| Item do plano | Estado | Impacto |
| --- | --- | --- |
| `REVIEW_REQUIRED` (revisão humana obrigatória) | **ausente** | o fluxo termina em `completed`, `completed_with_warnings`, `failed` ou `cancelled`; ambiguidades são registradas como `issue`, não como bloqueio |
| `POST /conversions/:id/review` (registrar correção humana) | **ausente** | não há tabela, DTO nem rota de revisão; nenhuma intervenção humana é persistida |

Implementá-los exige alterar o enum de status em `packages/contracts`,
`apps/api`, `apps/web`, `services/converter/api` e no banco (com migração) —
mudança de alto risco, deliberadamente não aplicada.

Ausente também: persistência da trilha de auditoria no gateway. Em modo `full`,
o NestJS é proxy da auditoria do motor; se o banco do motor for perdido, a
trilha não é recuperável pela aplicação (os eventos de **progresso** são
persistidos no Prisma).

## Modo standalone

Quando `RUNTIME_MODE=standalone`, o FastAPI expõe exatamente os mesmos
endpoints públicos em `/api/v1`, usa SQLite, storage local e worker inline.
O contrato não muda — apenas o processo que responde.

## Modo full (gateway)

O NestJS mantém o contrato legado de PDF→XLSX e adiciona rotas de paridade que
encaminham para o motor com `x-internal-token` e o `owner_id` já autenticado:

- `GET /capabilities`, `GET /models/status`;
- `GET|POST /conversions/:id/exports`, `GET .../exports/:exportId/download`;
- `GET /conversions/:id/text`;
- `/knowledge/*` e `/chat/*` (incluindo o stream SSE de mensagens).

O gateway valida o usuário antes de encaminhar; o motor aplica a ACL pelo
`owner_id` recebido no canal interno. Upload de imagem também é aceito: a
detecção é por assinatura e o alvo é reduzido a TXT.
