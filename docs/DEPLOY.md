# Execução com containers (local)

Este guia descreve como rodar o Conversor com containers na sua própria máquina.
O projeto é local: não há publicação na internet, túnel nem perfil público. Para
rodar sem containers (standalone local ou modo completo com NestJS), veja o
[README](../README.md). O fluxo de execução está em [FLOW.md](FLOW.md), a lista
completa de variáveis em [CONFIGURATION.md](CONFIGURATION.md), onde os arquivos
são gravados em [STORAGE.md](STORAGE.md) e a operação diária (cópias de segurança,
restauração) em [OPERATIONS.md](OPERATIONS.md).

O compose publica a interface somente em `127.0.0.1`: ela responde apenas nesta
máquina. Nenhum outro serviço é publicado no host. Para acesso por outro
dispositivo da rede local, veja [Acesso pela rede local](#acesso-pela-rede-local).

Os comandos e variáveis citados existem no repositório. Nenhum comando
administrativo é executado por este documento: instalação e subida são passos do
operador.

---

## Opções

| Opção | Quando usar | Observações |
| --- | --- | --- |
| **A. Docker Compose (`infra/docker-compose.yml`)** | stack completa na sua máquina, com PostgreSQL, Redis, MinIO, motor, worker Celery, API e interface | somente o `web` publica uma porta, em `127.0.0.1:${WEB_PORT:-8080}`; ele encaminha `/api/v1` para a API, que não publica porta no host. A composição não tem execução registrada neste repositório (veja [TESTING.md](TESTING.md)). |
| **B. Container único do motor (`services/converter/Dockerfile`)** | um processo FastAPI em `RUNTIME_MODE=standalone`, com SQLite, disco local e fila `inline` | a imagem contém apenas `services/converter/`. Ela serve `/api/v1` e as rotas internas, **sem a interface**: `apps/web/dist` não é copiado, então o código não encontra o build da SPA. Não há NestJS nem PostgreSQL. A API é a mesma (`docs/API.md`). |

A imagem do motor (opção B) só serve a interface se `apps/web/dist` for copiado
para dentro dela, o que o Dockerfile atual não faz. Para usar a interface sem o
compose, rode o modo standalone sem container, como descrito no README.

Os Dockerfiles devem ser construídos **a partir da raiz do repositório**, porque
ambos dependem de arquivos fora de sua pasta:

```bash
# API (NestJS). Para PostgreSQL, defina o build arg:
docker build -f apps/api/Dockerfile --build-arg DATABASE_PROVIDER=postgres -t pdf2x-api .

# Motor (Python)
docker build -f services/converter/Dockerfile -t pdf2x-converter .
```

O build arg `DATABASE_PROVIDER` troca o schema do Prisma (de SQLite para
PostgreSQL) durante a construção da imagem. O cliente Prisma é específico de
cada provedor, por isso não pode ser trocado em tempo de execução. Em
PostgreSQL, a imagem também precisa de `DATABASE_PROVIDER=postgres` em tempo de
execução, para que o `docker-entrypoint.sh` aplique o schema com
`prisma db push` a cada inicialização.

A imagem do motor instala `".[full,postgres]"`. Ela inclui OpenCV, pytesseract,
Celery, Redis, boto3 e psycopg. **Não** inclui os extras `ocr` (RapidOCR),
`docling`, `migrate` (Alembic) nem `test`.

### Variáveis de ambiente do backend

Defina estas variáveis no `infra/.env` (compose) ou no ambiente do container. Não
faça commit de valores reais.

**Essenciais**

| Variável | Serviço | Valor |
| --- | --- | --- |
| `RUNTIME_MODE` | ambos | `full` (A) ou `standalone` (B) |
| `JWT_SECRET` | api (no compose, só a API recebe; veja [Segredos no compose](#jwt_secret-no-compose)) | segredo aleatório, 64 caracteres hexadecimais |
| `CONVERTER_INTERNAL_TOKEN` | api e motor | **o mesmo valor** nos dois serviços |
| `ALLOWED_ORIGINS` | api e motor | origem da interface, por exemplo `http://localhost:8080` (padrão do compose), separadas por vírgula, sem barra final |
| `CONVERTER_INTERNAL_URL` | api | URL do motor na rede interna, por exemplo `http://converter:8000` (modo `full`) |
| `API_HOST` | api | `0.0.0.0` no compose, para que o `web` alcance a API dentro da rede do compose. Fora de container o padrão do NestJS é `127.0.0.1` |
| `API_POSTGRES_URL` | api | `postgresql://usuario:senha@host:5432/pdf2x_api?schema=public` |
| `CONVERTER_DATABASE_URL` | motor | `postgresql+psycopg://usuario:senha@host:5432/pdf2x_conv` |
| `DATABASE_PROVIDER` | api | `postgres` (com o mesmo build arg na imagem) |
| `STORAGE_BACKEND` | api e motor | `s3` (no compose, via MinIO) ou `local` (ver abaixo) |

**Armazenamento**

- Com `STORAGE_BACKEND=s3`, configure `S3_ENDPOINT`, `S3_BUCKET`,
  `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` e `S3_SECURE`. Para MinIO, o
  compose define `S3_FORCE_PATH_STYLE=true` no serviço `api`.
- Com `STORAGE_BACKEND=local`, a API **e o motor** leem e gravam o mesmo
  diretório. O motor recebe a chave de armazenamento da API e lê o PDF dela. Por
  isso, os dois serviços precisam compartilhar o mesmo volume, no mesmo caminho
  (`STORAGE_LOCAL_ROOT`). Em dois serviços separados, isso normalmente exige S3.
  A opção local é mais simples apenas no modo B.
- Chaves e estrutura de pastas do storage: [STORAGE.md](STORAGE.md).

**Motor e fila**

| Variável | Recomendação |
| --- | --- |
| `CONVERTER_QUEUE` | `inline` (padrão, threads no próprio processo) ou `celery` (requer Redis e um worker separado, como no compose). Leia [Limitações do modo Celery](#limitações-do-modo-celery) antes de escolher |
| `CONVERTER_WORKERS` | concorrência do pool `inline` |
| `CONVERTER_TASK_TIMEOUT_SECONDS` | `0` (padrão): **sem prazo global por documento**. Cada chamada de OCR tem seu próprio limite (`CONVERTER_OCR_CALL_TIMEOUT_SECONDS`, padrão `300`). Um valor maior que `0` impõe um teto de tempo ao documento inteiro |
| `CONVERTER_MAX_PDF_BYTES` | padrão `52428800` (50 MiB); ajuste conforme o seu hardware |

**Limites e retenção**

| Variável | Padrão | Observação |
| --- | --- | --- |
| `UPLOAD_MAX_BYTES` | `52428800` (50 MiB) | aplicado pela API e pelo motor. Um proxy intermediário pode impor limite menor: confira |
| `CONVERTER_MAX_PAGES`, `UPLOAD_MAX_PAGES` | vazio (sem teto) | **Legado.** O motor nunca recusa nem trunca por eles: no máximo registra aviso. No NestJS, `UPLOAD_MAX_PAGES` definida rejeita com `TOO_MANY_PAGES`. O compose não define nenhuma das duas |
| `AUTH_MODE` | `optional` | sessão por navegador, sem login. `required` exige Bearer nas rotas protegidas. `POST /auth/anonymous` responde 201 em qualquer modo, tanto no NestJS quanto no motor standalone. Não há tela de login |
| `RETENTION_PDF_DAYS`, `RETENTION_XLSX_DAYS`, `RETENTION_AUDIT_DAYS` | `7` / `30` / `90` | informados em `/settings`. Nenhum job aplica esses prazos; veja o item 10 do checklist |
| `LOG_LEVEL`, `LOG_JSON` | `INFO`, `1` nas imagens | |

**Lotes, upload por partes e checkpoints**

| Variável | Padrão | Papel |
| --- | --- | --- |
| `BATCH_UPLOAD_ENABLED` | `true` | Liga lotes e upload por partes. Desativar interrompe **novas admissões**, nunca execuções já iniciadas |
| `LARGE_DOCUMENT_PIPELINE_ENABLED` | `true` | Processamento do documento inteiro, página por página |
| `UPLOAD_PART_BYTES` | `8388608` | Parte de transporte (não é teto de documento) |
| `UPLOAD_PART_MAX_BYTES` | `33554432` | Maior parte aceita pela API |
| `UPLOAD_CONCURRENCY` | `3` | Lida pela configuração do NestJS e do motor, mas não é anunciada em `/capabilities` nem altera o paralelismo das rotas verificadas |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | Validade de sessão de upload incompleta |
| `MAX_FILES_PER_BATCH` | vazio (sem teto) | Só para operadores que quiserem limitar admissão |
| `UPLOAD_MAX_FILE_BYTES` | vazio (sem teto) | Idem, por documento |
| `CONVERTER_PAGE_WINDOW` | `25` | Lida, mas sem efeito (nenhum leitor no código) |
| `CONVERTER_OCR_CALL_TIMEOUT_SECONDS` | `300` | Timeout de **uma** chamada de OCR |
| `CHECKPOINT_REUSE_ENABLED` | `true` | Reuso de checkpoints de página quando a execução é retomada |
| `RESULT_COMPACT_ENABLED` | `true` | Tela de resultado compacta (API) |
| `MCP_ENABLED` | `true` | Anuncia o adaptador MCP em `/settings` (API) |

**OCR e assistente**

- **Tesseract** já vem instalado na imagem do motor (`tesseract-ocr` e
  `tesseract-ocr-por`). Não é preciso configurar nada para ele.
- **GLM-OCR** depende do Ollama. Dentro de um container, `GLMOCR_HOST=127.0.0.1`
  aponta para o próprio container, e não para a máquina host. O compose não repassa
  as variáveis `GLMOCR_*` ao motor, então, com a configuração padrão, o GLM-OCR fica
  indisponível dentro do compose. Para usá-lo, defina `GLMOCR_HOST` e `GLMOCR_PORT`
  com um endereço alcançável (por exemplo, `host.docker.internal`) no serviço
  `converter` e no `worker`, ou defina `GLMOCR_ENABLED=false`. Sem Ollama, páginas
  digitalizadas recorrem ao Tesseract ou falham de forma explícita. Os motores e a
  ordem de escolha estão em [CONVERSION.md](CONVERSION.md#motores-locais).
- **Ollama com GPU** depende de drivers e do runtime de GPU do Docker no seu host.
  Esse cenário não foi testado neste repositório.
- **Assistente** (`ASSISTANT_ENABLED`) vem desativado por padrão. Ative-o apenas
  com um Ollama acessível e o modelo `qwen3.5:4b` instalado.
- **Privacidade do OCR:** o motor fala com o Ollama diretamente, sem o cliente
  MaaS do SDK `glmocr`, que envia documentos à nuvem quando há `ZHIPU_API_KEY` no
  ambiente. Mantenha `GLMOCR_HOST` em loopback ou em rede privada e não defina
  `ZHIPU_API_KEY` nesta execução.

### Gerar segredos

```bash
openssl rand -hex 32      # JWT_SECRET
openssl rand -hex 32      # CONVERTER_INTERNAL_TOKEN
```

Os valores padrão do código (`dev-...-change-me`) são apenas para desenvolvimento.
O motor registra um aviso ao iniciar quando encontra `CONVERTER_INTERNAL_TOKEN` ou
`JWT_SECRET` com o padrão do código. O gateway NestJS usa outro padrão para
`JWT_SECRET` (`dev-only-insecure-secret-change-me`, em
`apps/api/src/config/configuration.ts`), que esse aviso não cobre: defina
`JWT_SECRET` explicitamente na API.

### JWT_SECRET no compose

O compose passa `JWT_SECRET` apenas à API (`${JWT_SECRET:?set JWT_SECRET}`). O
motor e o worker não recebem a variável. Por isso:

- ao iniciar, o `converter` registra o aviso de `JWT_SECRET` com o valor padrão.
  Esse aviso é esperado no compose e não indica falha;
- o compose não encaminha tráfego para as rotas do motor que validam JWT (o `web`
  só fala com a API), então o valor padrão do motor não é usado por usuários;
- se quiser eliminar o aviso, defina o mesmo `JWT_SECRET` também no serviço
  `converter`. Não é obrigatório.

### Rotação de segredos

- **`CONVERTER_INTERNAL_TOKEN`**: troque nos dois serviços (API e motor) no mesmo
  intervalo e reinicie ambos. Se a troca for feita em apenas um, as chamadas
  internas falham com `401`.
- **`JWT_SECRET`**: tokens já emitidos deixam de ser válidos. Os clientes obtêm
  uma nova sessão pelo fluxo de sessão local. Confirme isso no teste de fumaça
  após a troca.
- Não coloque segredos no repositório, nas imagens ou nos logs. As variáveis
  `VITE_*` são públicas, porque entram no bundle do navegador. Não coloque nelas
  nenhum segredo.

---

## Origem, cookies e endereço da API

- `ALLOWED_ORIGINS` é comparado por igualdade: use a origem exata da interface
  (no compose, `http://localhost:8080`), sem barra final e sem caminho. Depois de
  qualquer mudança, reinicie a API (o CORS é lido na inicialização).
- No compose, a interface e `/api/v1` são servidos na mesma origem pelo `web`,
  então não há CORS entre eles. Uso local dispensa HTTPS.
- O cookie de sessão (`pdf2x_device_id`) é sempre `HttpOnly; SameSite=Lax`. O
  frontend guarda a identidade em `localStorage` e no cookie, e envia
  `x-device-id` em toda requisição. Por isso limpar apenas o cache não perde o
  histórico; limpar cookies **e** dados do site cria uma sessão nova, sem
  recuperar os arquivos antigos. Não há conta, senha ou login.

A resolução do endereço da API segue esta ordem:

1. `VITE_API_BASE`, gravado no bundle em tempo de build;
2. `/api/v1` (mesma origem, usado no desenvolvimento local e no compose).

### Acesso pela rede local

Por padrão a interface só responde em `127.0.0.1`. Para abri-la de outro
dispositivo da mesma rede, a mudança precisa ser feita de propósito: em
`infra/docker-compose.yml`, no serviço `web`, troque
`"127.0.0.1:${WEB_PORT:-8080}:80"` por `"${WEB_PORT:-8080}:80"` e suba o compose de
novo.

> **Atenção.** Nessa configuração qualquer dispositivo que alcance a porta usa a
> API sem login (`AUTH_MODE=optional`) e pode enviar arquivos. O compose não tem
> HTTPS. Antes de fazer a mudança, restrinja a porta no firewall do host ao
> endereço dos dispositivos que devem acessar. Como a interface chama a API pela
> mesma origem, `ALLOWED_ORIGINS` não precisa mudar para esse acesso. Desfaça a
> mudança quando não precisar mais dela.

---

## Persistência

Os dados ficam em volumes nomeados do compose. A estrutura de chaves e de pastas
do storage está em [STORAGE.md](STORAGE.md). Cópias de segurança e restauração
estão em [OPERATIONS.md](OPERATIONS.md).

| Volume | Serviço | O que guarda |
| --- | --- | --- |
| `pgdata` | `postgres` | os bancos `pdf2x_api` (API) e `pdf2x_conv` (motor, runs, auditoria em tabelas) |
| `miniodata` | `minio` | o bucket `pdf2x`: PDFs de entrada, XLSX e `runs/<run_id>/audit.json.gz` |
| `redisdata` | `redis` | Redis com `appendonly yes`: broker do Celery (`REDIS_URL`) |

Sem volume próprio no compose:

- o diretório de trabalho do motor (`data/work`, com cópias temporárias do PDF
  durante a execução) fica dentro do container e pode ser descartado;
- o `converter` e o `worker` não publicam nem guardam nada além do banco e do
  bucket.

Notas para o modo B (container único) e para o storage local:

- Nas imagens, os caminhos padrão são relativos a `/app` (`./data/...`). Defina
  caminhos absolutos (por exemplo, `STORAGE_LOCAL_ROOT=/data/storage` e
  `CONVERTER_DATABASE_URL=sqlite:////data/converter.db`) e monte o volume nesse
  caminho. A imagem do motor cria `/data/storage`, mas o padrão do código não
  aponta para ele.
- **SQLite na imagem da API**: o `docker-entrypoint.sh` aplica o schema apenas
  para PostgreSQL. Com SQLite, as tabelas precisam existir antes do primeiro uso
  (por exemplo, com `npx prisma db push` executado no container). Por isso, em
  containers recomendamos PostgreSQL.
- As tabelas do motor são criadas na inicialização (`init_db`). Para bancos
  existentes ou ambientes controlados, use as migrações Alembic de
  [Migrações de banco](#migrações-de-banco).

---

## Infra do compose (A)

- O compose sobe `postgres:16-alpine`, `redis:7-alpine`, `minio`, `minio-init`
  (executa uma vez para criar o bucket), `converter`, `worker` (Celery), `automation`,
  `api` e `web`. O `web` é o ponto de entrada da interface; API, motor, banco, Redis e
  MinIO ficam na rede interna `backend`.
- O serviço `automation` roda `python -m automation.cli` (mesma imagem do motor, sem
  HTTP). Ele liga `AUTOMATION_EXECUTOR_ENABLED=true` e envia os efeitos pelo gateway
  NestJS (`CONVERTER_PUBLIC_URL=http://api:3000`). Ele monta a pasta do host definida
  por `SOURCE_ROOTS_DIR` (padrão `./sources`, relativo a `infra/`) como `/sources`
  somente leitura. Registre as raízes dentro do container como `/sources/<nome>`, não
  como caminhos do host. O `converter` não deve ligar o executor ao mesmo tempo, para
  haver um único calendário.
- Somente o `web` publica uma porta (`WEB_PORT`, padrão `8080`, em loopback). Para
  acessar a API diretamente do host (por exemplo, com `curl`), adicione um `ports`
  ao serviço `api` ligado em loopback, como `127.0.0.1:3000:3000`.
- O `web` encaminha `/api/v1` para `API_UPSTREAM` (`http://api:3000`, pelo template
  de nginx em `apps/web/nginx.conf.template`).
- Troque as credenciais padrão do MinIO (`minioadmin`) e do PostgreSQL (`pdf2x`)
  antes de guardar documentos que você queira preservar ou de abrir alguma porta a
  outras máquinas.

### Limitações do modo Celery

Com `CONVERTER_QUEUE=celery` (como no compose) o trabalho é entregue ao worker
Celery por `send_task`. Esse caminho tem menos garantias do que o modo `inline`:

- **Sem linha em `engine_jobs`.** `CeleryQueue.submit` não grava a tabela de jobs
  duráveis. Lease, fence e reentrega dessa tabela existem apenas no modo `inline`.
- **Sem retomada por checkpoint.** O worker Celery não passa `resume` a
  `execute_run` (`services/converter/worker/celery_tasks.py`). O modo `inline`
  passa `resume` quando a tentativa é maior que 1. Por isso, depois de uma falha,
  o documento é reprocessado do zero no modo Celery, sem reaproveitar páginas já
  extraídas.
- **Cancelamento por revoke.** `CeleryQueue.cancel` envia `revoke` com
  `terminate=True` e sinal `SIGTERM`. A checagem por Redis
  (`pdf2x:cancel:<run_id>`, em `is_cancelled`) lê uma chave que nenhum código fora
  dos testes grava. Na prática, o cancelamento depende do revoke.

Hoje, a retomada por checkpoint e a reentrega com fence só são implementadas pelo
modo `inline`. Ao escolher `celery`, considere que uma falha no meio de um
documento exige nova execução completa.

---

## Limitações conhecidas

- **Identidade sem login**: a identidade é o valor aleatório guardado no
  navegador. Quem copiar esse valor assume a sessão. Não há limite por IP nem
  expurgo automático de retenção.
- **Fallback de SSE por `EventSource`**: o caminho principal de progresso usa
  `fetch` com streaming, e envia `x-device-id` e `Cache-Control` no cabeçalho.
  Esse caminho funciona entre origens porque a API libera `Cache-Control` no
  CORS. O fallback para `EventSource`
  (`apps/web/src/hooks/use-conversion-events.ts`) não consegue enviar
  `x-device-id` em requisição cross-origin. Ele só é usado quando o
  `fetch` falha; nesse caso o cabeçalho de identidade não é enviado e o
  fallback deve ser tratado como degradado. A consulta de status a cada 5 s
  (`useConversion`) mantém a tela atualizada mesmo sem o stream.
- **`ALLOWED_ORIGINS` do motor**: o motor Python também lê `ALLOWED_ORIGINS`.
  Inclua a origem da interface nele, além da API, para que o navegador possa
  chamar o motor diretamente, se isso for usado.
- **Modo Celery**: veja [Limitações do modo Celery](#limitações-do-modo-celery).

## Checklist de execução com containers

Execute na ordem:

1. **Saúde da API**: `GET /health` na API responde `200` com `"status": "ok"` e
   todos os serviços `up`. Se algum estiver `down`, verifique a conexão com o
   banco, o storage e o motor.
2. **Motor interno**: a porta do motor não deve ser publicada no host. Confira em
   `docker compose ps` e no `ports:` do compose que só o `web` (e, se você
   adicionou, a `api` em `127.0.0.1`) aparecem. As rotas `/internal/v1/*` exigem
   `x-internal-token`.
3. **Origem liberada**: a interface carrega a página sem erros de CORS no console
   do navegador. Se houver erro, confira `ALLOWED_ORIGINS`.
4. **Frontend apontando para a API certa**: a aba de rede do navegador mostra
   chamadas para `/api/v1/...` na mesma origem da interface, ou para o endereço
   definido em `VITE_API_BASE`.
5. **Teste de fumaça**: envie um PDF pequeno com texto nativo. Acompanhe o
   progresso por SSE, baixe o XLSX e abra a auditoria.
6. **Teste de digitalizado** (se houver OCR configurado): envie uma página sem
   camada de texto e confirme que o resultado é recuperado ou que a falha é
   explícita.
7. **Logs**: o motor não deve registrar aviso de `CONVERTER_INTERNAL_TOKEN` com o
   valor padrão: se aparecer, troque o segredo. Um aviso só de `JWT_SECRET` no
   motor é esperado no compose (veja [JWT_SECRET no compose](#jwt_secret-no-compose)).
   Os logs não devem conter conteúdo de células (verifique em uma conversão de teste).
8. **Portas do host**: apenas o `web` (e a API, se você a expôs em loopback)
   aparece como porta publicada, e o `web` deve aparecer como `127.0.0.1:…`.
   PostgreSQL, Redis, MinIO e o motor ficam na rede interna.
9. **Cópias de segurança**: o PostgreSQL e o bucket (ou o volume) têm cópia de
   segurança e restauração testada. Procedimento em [OPERATIONS.md](OPERATIONS.md).
10. **Retenção**: confirme como os prazos de `RETENTION_*` serão aplicados. O
    código informa os prazos, mas nenhum job os aplica.
    Se precisar que os arquivos sejam apagados nos prazos, defina a rotina
    operacional. A exclusão feita por `DELETE /conversions/:id` é lógica. No motor
    standalone remove `output.xlsx` e `audit.json.gz`; no NestJS remove o PDF original
    quando nenhum outro documento usa a mesma chave e a conversão está em estado terminal.
11. **Limites**: confirme que o limite do proxy do `web` aceita o
    `UPLOAD_MAX_BYTES` configurado.

---

## Migrações de banco

### Banco da API (Prisma)

O projeto usa `db push`. Para ambientes versionados, aplique as mudanças aditivas
introduzidas pelos lotes e pela fila, em ordem:

- `Batch`, `BatchItem`, `UploadSession`, `UploadPart`, `BatchEvent`;
- `Conversion.optionsJson`;
- `OutboxEvent.lockedBy`, `OutboxEvent.lockedUntil`.

Nada é destrutivo: conversões antigas ficam sem `optionsJson` e sem lote, e
continuam legíveis. Faça cópia de segurança verificável antes e teste a
restauração em um ambiente isolado.

### Banco do motor (Alembic)

Execute a partir de `services/converter`. O comando `alembic` exige o extra
`migrate`, que não vem na imagem do motor:

```powershell
.venv\Scripts\python -m pip install -e ".[migrate]"
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic check   # deve dizer "No new upgrade operations detected"
```

Revisões adicionadas: `c4f1a2b7d910` (lotes/uploads), `6d13f972f6c5` (tabelas de
conhecimento/exportações e `options_json` que faltavam), `e08fecd3c89f`
(`engine_jobs`, checkpoints e `runs.cancel_requested_at`).

---

## Rollout e rollback

1. Suspenda novas admissões (`BATCH_UPLOAD_ENABLED=false`).
2. Deixe os workers incrementais concluírem ou pausarem com checkpoint.
3. Volte a versão anterior da API/UI sem apagar tabelas novas.
4. Runs iniciados no modelo novo precisam do worker correspondente até o estado
   terminal; o binário antigo não deve retomá-los.

O primeiro mecanismo de rollback é **flag + drenagem + versão compatível**, não
restauração destrutiva de schema.
