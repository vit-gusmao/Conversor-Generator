# Onde ficam os dados

Este documento responde a uma pergunta: **quando o Conversor Generator roda na sua máquina, o que é gravado, onde, por quem, e o que acontece com cada coisa depois**.

O projeto é 100% local. Nada é enviado para fora da máquina por padrão, e as portas são publicadas apenas em `127.0.0.1`. Os dados vivem em três lugares: arquivos sob `services/converter/data/` (modo standalone), bancos e armazenamento da API NestJS (modo full) e volumes Docker (compose). O navegador guarda apenas identidade e metadados de fila.

Documentos relacionados: [FLOW.md](FLOW.md) (fluxo completo), [CONFIGURATION.md](CONFIGURATION.md) (variáveis de ambiente), [DATABASE.md](DATABASE.md) (esquema de tabelas), [DEPLOY.md](DEPLOY.md) (containers locais) e [OPERATIONS.md](OPERATIONS.md) (backup, limpeza e diagnóstico).

---

## 1. Resumo

### 1.1 Tabela de referência

| O que | Onde (padrão) | Quem grava | Quando é apagado |
|---|---|---|---|
| Banco do conversor (SQLite) | `services/converter/data/converter.db` (+ `-wal`, `-shm`) | converter (SQLAlchemy) | Nunca automaticamente. Só por reset manual. |
| Partes de upload em andamento | `services/converter/data/storage/uploads/<upload_id>/` | converter (`put_part`) | No `abort`. Após `complete` as partes **permanecem** no disco. Nunca por tempo. |
| Documento original (deduplicado por dono) | `services/converter/data/storage/documents/<owner_id>/<sha256>.<ext>` | converter (`complete` e `create`) | Nunca no standalone. Na API NestJS, só quando a conversão é excluída e nenhum outro documento usa a mesma chave. |
| Planilha final | `.../storage/runs/<run_id>/output.xlsx` | converter (repositório de conversão) | Na exclusão lógica da conversão (melhor esforço). |
| Auditoria comprimida | `.../storage/runs/<run_id>/audit.json.gz` | converter | Na exclusão lógica da conversão (melhor esforço). |
| Plano de texto, exportações, checkpoints | `.../storage/runs/<run_id>/text_plan.json.gz`, `exports/`, `pages/` | converter | Nunca automaticamente. |
| Arquivos de trabalho do motor | `services/converter/data/work/` | worker / motor | Ao fim do run (cópias de trabalho). |
| Banco da API (full, SQLite) | `apps/api/prisma/data/api.db` | Prisma (NestJS) | Nunca automaticamente. |
| Armazenamento da API (full, local) | `apps/api/data/storage/` | NestJS | Veja seção 6. |
| Postgres, MinIO, Redis (compose) | volumes Docker `pdf2x_pgdata`, `pdf2x_miniodata`, `pdf2x_redisdata` | containers | Só com `docker compose down -v`. |
| Identidade e histórico do MCP | `~/.pdf2x/` (ou `PDF2X_CONFIG_DIR`) | adaptador MCP | Nunca automaticamente. |
| Identidade e fila no navegador | `localStorage`, cookies, IndexedDB | aplicação web | Ao limpar os dados do site. |

### 1.2 Árvore de diretórios (standalone)

Árvore completa do modo standalone, com a base em `services/converter/data/`:

```text
services/converter/data/
├── converter.db                 # SQLite (modo WAL)
├── converter.db-wal             # log de escrita; ativo enquanto o servidor roda
├── converter.db-shm            # memória compartilhada do WAL
├── storage/                     # STORAGE_LOCAL_ROOT (padrão ./data/storage)
│   ├── documents/
│   │   └── <owner_id>/
│   │       └── <sha256>.<ext>   # documento original, imutável
│   ├── uploads/
│   │   └── <upload_id>/
│   │       ├── incoming/
│   │       │   └── <NNNNN>-<uuid>.part   # parte recém-recebida (temporário)
│   │       ├── <NNNNN>.part              # parte confirmada (5 dígitos)
│   │       └── assembled.bin             # montagem antes da verificação (temporário)
│   └── runs/
│       └── <run_id>/
│           ├── output.xlsx
│           ├── text_plan.json.gz
│           ├── audit.json.gz
│           ├── exports/
│           │   └── <export_id>.<ext>
│           └── pages/
│               └── <stage>/
│                   └── <NNNNNN>.json.gz  # checkpoint por página (6 dígitos)
└── work/                        # arquivos de trabalho do motor
    ├── <run_id>.source          # só com backend não local (cópia materializada)
    ├── <run_id>.<ext>           # cópia renomeada, se a extensão divergir do conteúdo
    └── <run_id>.derotated.pdf   # cópia com /Rotate normalizado (temporária)
```

As chaves de objeto sempre são relativas à raiz do storage. Um mesmo `.part` ou `.json.gz` aparece com o mesmo caminho no backend local e, em S3/MinIO, como chave dentro do bucket.

---

## 2. Modo standalone (converter)

### 2.1 Como os caminhos são resolvidos

Os caminhos relativos do converter são resolvidos a partir do diretório `services/converter/`, e **não** do diretório de trabalho do processo. Isso vem de `SERVICE_DIR` em [services/converter/config.py](../services/converter/config.py):

| Variável | Padrão | Resolução |
|---|---|---|
| `CONVERTER_DATABASE_URL` | `sqlite:///./data/converter.db` | `./data/converter.db` vira `services/converter/data/converter.db` |
| `STORAGE_LOCAL_ROOT` | `./data/storage` | vira `services/converter/data/storage` |
| diretório de dados (`data_dir`) | pai do arquivo SQLite | `services/converter/data` |
| `work/` | `data_dir / "work"` | `services/converter/data/work` |

Caminhos absolutos são respeitados sem alteração. `get_settings()` cria `data_dir` e `storage_dir` na importação, por isso a pasta reaparece no próximo start.

Se `CONVERTER_DATABASE_URL` não for SQLite (por exemplo Postgres no compose), `data_dir` cai em `services/converter/data`, e `work/` continua sendo criado ali.

### 2.2 Banco de dados

O SQLite roda em modo WAL. `init_db()` executa `PRAGMA journal_mode=WAL` e `PRAGMA foreign_keys=ON`, então o diretório tem três arquivos:

- `converter.db`: páginas do banco.
- `converter.db-wal`: alterações ainda não copiadas para o arquivo principal. Pode ser grande (vários MB) com o servidor ativo.
- `converter.db-shm`: índice de memória compartilhada do WAL. É recriado pelo SQLite.

Para o esquema das tabelas, veja [DATABASE.md](DATABASE.md). Para a lista por grupo, veja a seção 8.

### 2.3 Objetos (documentos, partes, saídas)

A implementação local está em [services/converter/storage/local.py](../services/converter/storage/local.py). Cada escrita segue o mesmo padrão:

1. Grava em arquivo temporário no mesmo diretório (`*.part`, `*.compose`, `*.tmp`).
2. Faz `fsync`.
3. Usa `os.replace` para tornar o objeto visível sob a chave final.

Se o processo cair no meio, nenhum arquivo parcial aparece sob a chave definitiva. Pode sobrar apenas um temporário.

Chaves gravadas pelo converter:

| Chave | Gravada em | Observação |
|---|---|---|
| `documents/<owner_id>/<sha256><ext>` | [api/service.py](../services/converter/api/service.py) (`create`, `create_from_stored`) e [api/upload_service.py](../services/converter/api/upload_service.py) (`complete`) | `<ext>` é `.pdf` para PDF e `.jpg`, `.jp2`, `.tif` ou `.<fmt>` para imagens |
| `uploads/<upload_id>/incoming/<NNNNN>-<uuid>.part` | `upload_service.put_part` | recebimento com chave única, para uma retransmissão divergente nunca destruir uma parte já confirmada |
| `uploads/<upload_id>/<NNNNN>.part` | `upload_service.put_part` | parte confirmada |
| `uploads/<upload_id>/assembled.bin` | `upload_service.complete` | montagem; vira o documento final com `move` ou é apagada |
| `runs/<run_id>/output.xlsx` | [repositories/conversion_repository.py](../services/converter/repositories/conversion_repository.py) | saída principal |
| `runs/<run_id>/text_plan.json.gz` | idem | plano de texto |
| `runs/<run_id>/audit.json.gz` | idem | manifesto de auditoria |
| `runs/<run_id>/exports/<export_id>.<ext>` | `store_export` no mesmo repositório | exportações sob demanda |
| `runs/<run_id>/pages/<stage>/<NNNNNN>.json.gz` | [repositories/checkpoint_repository.py](../services/converter/repositories/checkpoint_repository.py) | checkpoint por página, reaproveitado em reexecução |

O documento é **deduplicado por dono**: `storage.exists(key)` impede uma segunda cópia do mesmo hash para o mesmo `owner_id`. O hash sozinho não identifica o arquivo entre donos diferentes (veja a seção 9).

### 2.4 Trabalho temporário (`work/`)

O worker ([worker/tasks.py](../services/converter/worker/tasks.py)) executa o run a partir de caminhos locais. Três situações:

| Arquivo | Quando existe | Quem remove |
|---|---|---|
| `work/<run_id>.source` | Somente quando o backend **não** é local: o `materialize` copia o objeto S3 para disco. No backend local, `materialize` devolve o próprio arquivo de `documents/` e nenhuma cópia é feita. | `finally` do worker, somente se o arquivo estiver em `work/` (não remove o objeto original) |
| `work/<run_id>.<ext>` | Quando a extensão do arquivo recebido não bate com o conteúdo detectado. Renomeado antes do motor rodar. | `finally` do worker |
| `work/<run_id>.derotated.pdf` | Quando há páginas com `/Rotate`. Cópia normalizada criada por [engine/pipeline.py](../services/converter/engine/pipeline.py). | `_cleanup_work_copy`, chamado ao fim de `run()`, inclusive em falha tratada |

> Atenção: no backend local, se a extensão divergir do conteúdo, o `replace` move o próprio objeto de `documents/` para `work/`, e o `finally` o remove. Na prática, os sufixos de upload e de worker são calculados da mesma forma, então o caso não ocorre hoje. Vale saber ao mexer em um dos dois lados.

### 2.5 Arquivos temporários de upload (%TEMP%)

Cada parte chega como stream. [api/upload_routes.py](../services/converter/api/upload_routes.py) usa `tempfile.SpooledTemporaryFile(max_size=1 MiB)`: até 1 MiB fica em memória, e acima disso o conteúdo vai para o diretório temporário do sistema (`%TEMP%` no Windows). O arquivo é fechado no `finally`, então o espaço é liberado mesmo em erro.

---

## 3. Modo full (NestJS + converter)

No modo full, quem atende o usuário é a API NestJS ([apps/api](../apps/api)). Ela tem seus próprios banco e armazenamento. O converter continua com o banco e o `data/` dele, porque guarda evidência, auditoria e saídas.

### 3.1 Banco da API (Prisma)

| Provider | Variável | Caminho padrão | Observação |
|---|---|---|---|
| SQLite | `API_DATABASE_URL=file:./data/api.db` | `apps/api/prisma/data/api.db` | O Prisma resolve caminhos relativos de SQLite a partir do **arquivo de schema** (`apps/api/prisma/schema.prisma`), não do diretório de trabalho. |
| PostgreSQL | `API_POSTGRES_URL` (schema `schema.postgres.prisma`) | banco `pdf2x_api` no Postgres | Usado no compose. |

Para SQLite, o schema padrão é [apps/api/prisma/schema.prisma](../apps/api/prisma/schema.prisma). A variante PostgreSQL é [apps/api/prisma/schema.postgres.prisma](../apps/api/prisma/schema.postgres.prisma), com os mesmos modelos. A troca de provider não é automática em runtime: a imagem Docker copia `schema.postgres.prisma` para `schema.prisma` no build, e localmente é preciso escolher o schema ao rodar o Prisma (veja [OPERATIONS.md](OPERATIONS.md)).

### 3.2 Armazenamento local da API

[apps/api/src/upload/local-storage.service.ts](../apps/api/src/upload/local-storage.service.ts) resolve a raiz com:

```ts
path.resolve(process.cwd(), config.storageLocalRoot)
```

Ou seja, `STORAGE_LOCAL_ROOT=./data/storage` é relativo ao **diretório de onde o processo foi iniciado**. Com `npm --prefix apps/api run ...` o cwd é `apps/api`, e o caminho fica em `apps/api/data/storage/`. Iniciado a partir da raiz do repositório, o caminho vira `<repo>/data/storage/`.

> Essa é uma diferença real com o converter, que resolve a mesma variável a partir de `services/converter/`. Veja a tabela da seção 3.4.

Chaves da API:

| Chave | Origem |
|---|---|
| `documents/<ownerId>/<ab>/<sha256>.<ext>` | `storageKeyForSha256` em [storage.service.ts](../apps/api/src/upload/storage.service.ts). `<ab>` são os dois primeiros caracteres do hash. `ownerId` é sanitizado. |
| `uploads/<uploadId>/<NNNNN>.part` | `uploadPartKey` |
| `uploads/<uploadId>/incoming/<NNNNN>-<nonce>.part` | `uploadIncomingKey` |
| `uploads/<uploadId>/assembled.bin` | `uploadStagingKey` |

Dois detalhes:

- A escrita usa arquivo temporário com sufixo `.part` ou `.compose` e `rename` no fim. A regra é a mesma do converter.
- A chamada `FileInterceptor` do multer, usada no envio legado de arquivo único, mantém o conteúdo **em memória** até o limite `UPLOAD_MAX_BYTES` (padrão 50 MiB). O envio por partes não carrega o documento inteiro na memória: cada parte é limitada por `UPLOAD_PART_BYTES` e gravada em disco.

### 3.3 Dados do converter no modo full

O converter continua com `services/converter/data/` quando o backend é local. No modo full com `STORAGE_BACKEND=s3`, os documentos e saídas vão para o bucket, mas `work/` e o SQLite do converter (se usado) continuam locais. No compose, o converter não tem volume próprio: `work/` fica dentro do container, em `/app/data/work`, e some quando o container é recriado.

### 3.4 Diferenças entre os dois lados

| Aspecto | Converter (`services/converter`) | API NestJS (`apps/api`) |
|---|---|---|
| Raiz relativa de `STORAGE_LOCAL_ROOT` | `services/converter/` | `process.cwd()` |
| Banco | `CONVERTER_DATABASE_URL` (SQLite ou Postgres `pdf2x_conv`) | Prisma, `API_DATABASE_URL` ou `API_POSTGRES_URL` |
| Chave de documento | `documents/<owner_id>/<sha256><ext>` | `documents/<ownerId>/<ab>/<sha256>.<ext>` |
| Limpeza de documento | nunca no standalone | ao excluir a conversão, se nenhuma outra linha usar a chave |
| Envio legado | não aplicável | multer em memória até `UPLOAD_MAX_BYTES` |

As chaves **não são as mesmas** entre os dois lados. Um documento gravado pela API não aparece no caminho de documentos do converter, e vice-versa. Por isso `runs/` e `documents/` precisam ser copiados juntos quando se faz backup do modo full (veja [OPERATIONS.md](OPERATIONS.md)).

### 3.5 `RUNTIME_MODE`

`RUNTIME_MODE` no converter **não muda o armazenamento nem o comportamento**: o valor só é exposto em `/api/v1/capabilities`. Quem decide o modo é o cliente que chama: a SPA em `standalone` fala com `/api/v1` do converter, e o modo full passa pela API NestJS. Os caminhos de disco descritos acima valem para os dois, com as diferenças da tabela 3.4.

---

## 4. Docker Compose

O arquivo [infra/docker-compose.yml](../infra/docker-compose.yml) define `name: pdf2x`. Por isso os volumes levam o prefixo `pdf2x_`.

| Volume | Montado em | Conteúdo |
|---|---|---|
| `pdf2x_pgdata` | `postgres:/var/lib/postgresql/data` | bancos `pdf2x_api` (NestJS) e `pdf2x_conv` (converter), criados por [infra/postgres/init.sql](../infra/postgres/init.sql) |
| `pdf2x_miniodata` | `minio:/data` | bucket `pdf2x` com as chaves `documents/`, `uploads/` e `runs/` |
| `pdf2x_redisdata` | `redis:/data` | AOF da fila Celery. Descartável: jobs pendentes podem ser reenfileirados pelo outbox. |

Containers `converter`, `worker`, `api` e `web` **não montam volumes**. Eles usam `STORAGE_BACKEND=s3` apontando para o MinIO. Só o `web` publica porta, em `127.0.0.1:${WEB_PORT:-8080}`.

Localizando os volumes:

```bash
docker volume ls --filter name=pdf2x
docker volume inspect pdf2x_pgdata
docker system df -v
```

```powershell
docker volume ls --filter name=pdf2x
docker volume inspect pdf2x_pgdata
docker system df -v
```

`docker volume inspect` mostra o `Mountpoint` no host. No Docker Desktop do Windows, esse caminho fica dentro da VM do WSL2 e não é acessível diretamente. Para copiar os dados, use os comandos de backup de [OPERATIONS.md](OPERATIONS.md).

---

## 5. S3 / MinIO (opcional)

Com `STORAGE_BACKEND=s3`, o converter ([storage/s3.py](../services/converter/storage/s3.py)) e a API gravam **as mesmas chaves lógicas** no bucket `S3_BUCKET` (padrão `pdf2x`). A estrutura dentro do bucket é a da seção 1.2, sem a raiz `storage/`.

Isso não significa nuvem. Quando `S3_ENDPOINT` aponta para o MinIO do compose (`http://minio:9000`), tudo continua local. Para usar um MinIO local fora do compose, aponte `S3_ENDPOINT` para `http://127.0.0.1:9000`. O projeto não tem configuração para AWS e não envia nada para serviços externos.

O `work/` continua local mesmo com S3: `materialize` baixa o objeto para `work/<run_id>.source` antes do run (veja a seção 2.4).

### 5.1 Erros do S3

O mapeamento de erros é diferente em cada processo:

- **Gateway** (`apps/api/src/upload/s3-storage.service.ts`): ausência do objeto
  (`NoSuchKey`, `NotFound` ou HTTP 404) vira `404 NOT_FOUND` com
  `reason: storage_object_not_found` nas leituras. `exists` devolve `false` nesse
  caso. Qualquer outra falha vira `503 STORAGE_UNAVAILABLE`, com `backend: s3` e a
  operação (`put`, `get`, `exists`, `put_stream`, `open_read`, criação de envio
  em partes).
- **Motor** (`services/converter/storage/s3.py`): `exists` e `delete` devolvem
  `False` em qualquer erro, sem distinguir causa. `get_bytes` e `open_read` deixam
  a exceção do boto3 subir. Nas rotas que leem o objeto, isso chega ao cliente
  como `500 INTERNAL`, e não como `404` ou `503`.

O backend local (`LocalStorage`) não tem esse mapeamento: `exists` devolve
`False` quando o arquivo não existe.

---

## 6. Ciclo de vida e retenção

### 6.1 O que as variáveis de retenção fazem (honesto)

`RETENTION_PDF_DAYS` (padrão 7), `RETENTION_XLSX_DAYS` (30) e `RETENTION_AUDIT_DAYS` (90) existem em [config.py](../services/converter/config.py) e em [configuration.ts](../apps/api/src/config/configuration.ts), e são expostos em `/api/v1/settings` como informação.

**Nenhum job executa essas regras.** Não há scheduler de limpeza, cron ou varredura que leia esses valores. Os campos `retention_days` gravados em `artifacts` e `export_artifacts` são apenas metadados. Ou seja, **hoje nada é apagado por idade**.

Outros comportamentos com a mesma característica:

- **Partes de upload não são apagadas após `complete`**. Nos dois lados (converter e NestJS), a montagem cria o documento final e as partes permanecem. Um documento enviado por partes passa a ocupar aproximadamente **2 vezes o tamanho do arquivo** (partes mais documento final) até alguém limpar manualmente.
- **Sessões de upload expiram só na prática**. `UPLOAD_SESSION_TTL_HOURS` (padrão 24) é verificado no `PUT` de parte, que passa a responder `410` no standalone e `409 UPLOAD_EXPIRED` no gateway. No gateway, o `complete` também verifica a expiração (`409 UPLOAD_EXPIRED`) antes de montar o documento. No standalone, o `complete` não verifica: uma sessão expirada com todas as partes ainda pode ser concluída. Não há varredura de sessões antigas.
- **Exclusão de conversão é lógica**. A linha recebe `deleted_at` em `public_conversions`, e só `runs/<run_id>/output.xlsx` e `runs/<run_id>/audit.json.gz` são removidos (melhor esforço, erros ignorados).

### 6.2 O que sobra no disco

| Item | Após excluir a conversão | Após o run terminar | Observação |
|---|---|---|---|
| Linhas do banco (`public_conversions`, `runs`, eventos, auditoria) | permanecem (soft delete) | permanecem | Veja [DATABASE.md](DATABASE.md). |
| `documents/<owner>/<sha>.<ext>` | **permanece** (standalone) | permanece | Na API NestJS, é removido se nenhum outro documento usa a chave. |
| `uploads/<id>/<NNNNN>.part` de sessões concluídas | não se aplica | **permanecem** | Só apagados em `abort`. |
| `uploads/<id>/assembled.bin` | n/a | removido (movido para `documents/`) | Em falha de hash, de cabeçalho ou de codec, é apagado. |
| `runs/<id>/output.xlsx` | **removido** | permanece | Remoção melhor esforço. |
| `runs/<id>/audit.json.gz` | **removido** | permanece | Remoção melhor esforço. |
| `runs/<id>/text_plan.json.gz` | permanece | permanece | |
| `runs/<id>/exports/*` | permanecem | permanecem | |
| `runs/<id>/pages/**/*.json.gz` (checkpoints) | permanecem | permanecem | Só reaproveitados na mesma reexecução. |
| `work/<run>.derotated.pdf` | n/a | **removido** | Permanece só se o processo for morto no meio. |
| `work/<run>.source` e `work/<run>.<ext>` | n/a | removidos pelo `finally` | Existem só com backend S3 ou com renomeação. |
| Temporários de `%TEMP%` (`SpooledTemporaryFile`) | n/a | removidos | Fechados no `finally`. |

### 6.3 Exclusão no NestJS

A exclusão ([conversions.service.ts](../apps/api/src/modules/conversions/conversions.service.ts)) marca `deletedAt` e, **somente se a conversão estiver em estado terminal**, remove o PDF quando nenhum outro documento referencia a mesma chave. Assim um arquivo ainda em execução não perde a entrada.

---

## 7. Navegador

A aplicação web ([apps/web](../apps/web)) guarda identidade e metadados de fila. **Nunca guarda os bytes dos arquivos.**

| Onde | Chave | Conteúdo | Quem grava | Duração |
|---|---|---|---|---|
| `localStorage` | `pdf2x.device_id` | identificador local gerado com `crypto.randomUUID` | [lib/api.ts](../apps/web/src/lib/api.ts) | até limpar os dados do site |
| Cookie (JS) | `pdf2x_device` | mesmo identificador; não é HttpOnly; `SameSite=Lax`; `path=/` | `lib/api.ts` | 1 ano |
| Cookie (servidor) | `pdf2x_device_id` | mesmo identificador; **HttpOnly**; `SameSite=Lax`; `path=/` | converter ([api/deps.py](../services/converter/api/deps.py)) ou NestJS ([auth.controller.ts](../apps/api/src/auth/auth.controller.ts)) | 1 ano |
| `localStorage` | `pdf2x.auth` | token e dados do usuário (Zustand `persist`, com `partialize` para `token` e `user`) | [stores/auth.ts](../apps/web/src/stores/auth.ts) | até logout ou limpeza |
| IndexedDB | banco `pdf2x-uploads`, store `items` (`keyPath: clientItemId`) | `clientItemId`, nome, tamanho, tipo, opções, estado, `uploadId`, `conversionId`, bytes enviados | [lib/upload-persistence.ts](../apps/web/src/lib/upload-persistence.ts) | até limpar os dados do site |

Pontos a observar:

- A identidade do navegador é reconstituída pelo cookie ou pelo `localStorage`. Se você apagar só um dos dois, o outro restaura a identidade. Veja a seção 9.
- A fila de uploads do IndexedDB é regravada inteira a cada mudança (`clear` e depois `put`).
- Após reload, itens que estavam em envio ou na fila viram `needs_reselection`, porque os bytes nunca foram persistidos: o usuário precisa selecionar o arquivo de novo.
- Para remover a sessão do navegador, basta limpar o `pdf2x.auth` (ou os dados do site). O servidor não guarda o token do navegador.

---

## 8. Banco de dados do converter

O esquema está em [services/converter/models/db.py](../services/converter/models/db.py). Grupos de tabelas:

| Grupo | Tabelas |
|---|---|
| Público (fronteira com a API) | `public_users`, `public_documents`, `public_conversions`, `public_events`, `public_batches`, `public_batch_items`, `public_batch_events`, `public_upload_sessions`, `public_upload_parts`, `idempotency_keys` |
| Motor e proveniência | `runs`, `page_results`, `regions`, `text_elements`, `extraction_candidates`, `decisions`, `extracted_fields`, `extracted_tables`, `output_sheets`, `output_cells`, `cell_evidence`, `audit_events`, `audit_issues`, `validation_results`, `coverage_rows`, `engine_usage`, `stage_timings`, `artifacts`, `export_artifacts` |
| Jobs e checkpoints | `engine_jobs`, `page_checkpoints`, `run_checkpoints` |
| Conhecimento e assistente | `knowledge_collections`, `knowledge_chunks`, `knowledge_index_jobs`, `conversations`, `chat_messages` |

Criação: `init_db()` roda `create_all` e, em seguida, aplica colunas aditivas que faltarem (hoje `public_conversions.options_json`). Não há Alembic obrigatório. Alembic existe para bancos persistentes já existentes, com o extra `migrate` (veja [OPERATIONS.md](OPERATIONS.md)).

Dois detalhes de SQLite:

- Os `artifacts` guardam o caminho da chave (`path`) e o hash, não o conteúdo.
- O `audit_events` tem encadeamento SHA-256 (`_verify_chain`). Apagar linhas no meio quebra a verificação.

---

## 9. Isolamento entre navegadores e donos

- As chaves de documento incluem o `owner_id`. Dois navegadores que enviam o **mesmo arquivo** geram **duas cópias**, uma por dono. Não há dedupe global.
- Dentro do mesmo dono, o mesmo hash não é gravado duas vezes.
- Como o hash não é um identificador público, um dono não consegue descobrir o caminho de outro.
- Se você limpar os dados do navegador, a identidade anônima muda e o servidor passa a enxergar **outro dono**. Os arquivos antigos continuam no disco, mas a interface deixa de mostrá-los. Não há recuperação pela aplicação: a identidade anônima é o único vínculo com o dono antigo, e o cookie `pdf2x_device_id` é HttpOnly, então não pode ser reposto manualmente. Quem perde a identidade perde o acesso aos documentos, mesmo que eles estejam no disco.

---

## 10. Arquivos em outros lugares

| Local | Conteúdo | Observação |
|---|---|---|
| `apps/web/dist/` | build da SPA | Gerado por `npm --prefix apps/web run build`. O converter serve esse diretório na raiz `/`. Ignorado pelo Git. |
| `<repo>/study/` | arquivos de estudo para o assistente | Só aceita `.md`, `.txt`, `.json` e `.csv`, até 4 MiB, por caminho explícito. Ignorado pelo Git (`/study/`). Veja [indexer.py](../services/converter/knowledge/indexer.py). |
| `*.log` | não há arquivos de log | Os logs vão para **stdout**. Veja [OPERATIONS.md](OPERATIONS.md) para redirecionar. |
| `*.part`, `*.compose`, `*.tmp` | temporários de escrita | Ficam ao lado do alvo, no mesmo diretório. |
| `.venv/` em `services/converter/` | ambiente Python | Não contém dados do usuário. |

---

## 11. Tamanho e crescimento

O que cresce, do maior para o menor impacto:

1. `storage/documents/` e `storage/uploads/` (partes não apagadas após `complete`).
2. `storage/runs/*/pages/` (checkpoints por página, em `json.gz`).
3. `converter.db` e `converter.db-wal`. O WAL é reduzido no checkpoint do SQLite, e o arquivo principal não encolhe sozinho.
4. `storage/runs/*/output.xlsx` e `exports/`.

Como medir:

```bash
du -sh services/converter/data/*
du -sh services/converter/data/storage/*
du -sh services/converter/data/storage/uploads
du -sh apps/api/prisma/data apps/api/data 2>/dev/null
```

```powershell
Get-ChildItem -Recurse -File "services\converter\data" |
  Measure-Object -Property Length -Sum |
  ForEach-Object { "{0:N1} MiB" -f ($_.Sum / 1MB) }

Get-ChildItem -Recurse -File "services\converter\data\storage\uploads" |
  Measure-Object -Property Length -Sum |
  ForEach-Object { "uploads: {0:N1} MiB" -f ($_.Sum / 1MB) }
```

Para limpar, veja a seção de limpeza em [OPERATIONS.md](OPERATIONS.md). Não apague nada dentro de `storage/` manualmente sem antes checar as tabelas da seção 6.2: o banco aponta para essas chaves.
