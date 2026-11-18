# Operação local: backup, limpeza, logs e diagnóstico

Este guia cobre o dia a dia de quem roda o Conversor Generator na própria máquina: subir e parar o servidor, verificar a saúde, ler logs, fazer backup, limpar espaço, aplicar migrações, atualizar e resolver problemas comuns.

Pré-requisito de leitura: [STORAGE.md](STORAGE.md) explica onde cada dado fica. Os nomes de variáveis estão em [CONFIGURATION.md](CONFIGURATION.md) e o fluxo de cada conversão em [FLOW.md](FLOW.md). Para containers, veja [DEPLOY.md](DEPLOY.md).

Os comandos assumem a raiz do repositório como diretório atual, salvo indicação contrária. Os exemplos de PowerShell usam Windows PowerShell 5.1; os de Bash servem para Git Bash.

> Comandos marcados com **[DESTRUTIVO]** apagam dados. Leia a seção de consequências antes de executar.

---

## 1. Iniciar e parar

Existem quatro formas de subir o sistema. Escolha uma, e não rode duas ao mesmo tempo na porta 8000.

### 1.1 `starter-server.cmd` (recomendado no Windows)

[starter-server.cmd](../starter-server.cmd) é o inicializador. Ele:

1. Verifica se o ambiente virtual existe em `services/converter/.venv`.
2. Consulta o GLM-OCR no Ollama. Esse passo é informativo e nunca bloqueia o start.
3. Se o servidor já responde em `http://127.0.0.1:8000/health`, apenas abre o navegador e sai.
4. Se a porta 8000 estiver ocupada por outro processo, mostra erro e para.
5. Se `apps/web/dist/index.html` não existir, tenta compilar a interface com `npm run build`.
6. Inicia `uvicorn` em uma janela minimizada chamada **"Conversor Generator - servidor"**, aguarda até 90 segundos e abre o navegador.

Para parar: feche a janela "Conversor Generator - servidor" ou pressione `Ctrl+C` nela.

Se faltar o ambiente virtual, o script orienta a criá-lo. A mensagem atual sugere `pip install -e ".[test]"`, que não instala as dependências de OCR. Para uso real, prefira:

```powershell
cd services\converter
python -m venv .venv
.\.venv\Scripts\pip install -e ".[full]"
```

### 1.2 Uvicorn manual

```powershell
cd services\converter
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --log-level info
```

```bash
cd services/converter
.venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --log-level info
```

Parar: `Ctrl+C` no terminal.

Para a interface, o servidor precisa de `apps/web/dist/`. Sem ele, a raiz `/` responde JSON em vez da página (veja a seção 8).

```powershell
npm --prefix apps\web run build
```

### 1.3 Modo full (API NestJS + converter)

Modo full exige dois processos: o converter na porta 8000 e a API NestJS na porta 3000. Use as mesmas credenciais internas nos dois lados.

```powershell
# Terminal 1: converter
cd services\converter
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000

# Terminal 2: API NestJS
npm --prefix apps\api run db:push
npm --prefix apps\api run build
npm --prefix apps\api run start:prod
```

`start:dev` (`nest start --watch`) recompila em alteração de arquivo e serve para desenvolvimento. `start:prod` executa `node dist/main.js` a partir de `apps/api/dist`, e exige o `build` anterior.

A API escuta em `API_HOST` (padrão `127.0.0.1`) e `API_PORT` (padrão `3000`). O converter precisa conhecer `CONVERTER_INTERNAL_URL` (padrão `http://localhost:8000`) e `CONVERTER_INTERNAL_TOKEN`.

### 1.4 Docker Compose

```bash
cd infra
docker compose up --build -d
docker compose ps
```

A interface fica em `http://127.0.0.1:8080`. Parar sem apagar dados:

```bash
cd infra
docker compose down
```

`docker compose down -v` também apaga os volumes. Veja a seção 5.4 antes de usar.

O compose exige as variáveis `CONVERTER_INTERNAL_TOKEN` e `JWT_SECRET`. Sem elas, o `docker compose up` falha com a mensagem de variável obrigatória.

---

## 2. Saúde e diagnóstico

### 2.1 Endpoints

| Serviço | Endpoint | O que verifica |
|---|---|---|
| Converter | `GET /health` | resposta simples com `status` e `version` (usado pelo `starter-server.cmd`) |
| Converter | `GET /api/v1/health` | banco (`SELECT 1`), fila e armazenamento. `status` é `ok` ou `degraded`. |
| Converter | `GET /internal/v1/health` | motor, fila, OCR e capacidades. Usado pelo healthcheck do container. Não exige token. |
| Converter | `GET /api/v1/health/ready` | `accepting_uploads`, `ocr_available` e `queue_pending`. Não expõe hosts nem caminhos. |
| Converter | `GET /docs` | documentação OpenAPI interativa |
| API NestJS | `GET /api/v1/health` | `db`, `queue`, `storage` e `converter`. Retorna `ok` ou `degraded`. |
| Compose | `docker compose ps` | estado e healthcheck de cada container |

A API NestJS expõe `/api/v1/health` (com o prefixo global) e também `GET /health` na raiz, um alias registrado em `apps/api/src/main.ts` para o healthcheck do contêiner. As duas rotas devolvem o mesmo relatório.

### 2.2 Exemplos (Windows PowerShell, `curl.exe`)

```powershell
curl.exe -s http://127.0.0.1:8000/health
curl.exe -s http://127.0.0.1:8000/api/v1/health
curl.exe -s http://127.0.0.1:8000/internal/v1/health
curl.exe -s http://127.0.0.1:8000/api/v1/health/ready
curl.exe -s http://127.0.0.1:3000/api/v1/health
```

Em Bash:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/health
curl -s http://127.0.0.1:8000/api/v1/health/ready
curl -s http://127.0.0.1:3000/api/v1/health
```

No compose, consulte dentro dos containers:

```bash
docker compose exec converter curl -fsS http://127.0.0.1:8000/internal/v1/health
docker compose exec api wget -qO- http://127.0.0.1:3000/api/v1/health
```

### 2.3 Como interpretar

- `degraded` em `/api/v1/health` quase sempre significa um serviço `down` dentro da resposta. Olhe o campo `services`.
- `accepting_uploads: false` em `/api/v1/health/ready` indica que o banco, a fila ou o armazenamento não responderam. A interface mostra "servidor offline" nesse caso.
- `ocr_available: false` não impede o uso. Páginas digitalizadas só deixam de ser reconhecidas (veja a seção 8).

---

## 3. Logs

### 3.1 Onde os logs vão

- **Converter**: somente para **stdout**. Não há arquivo de log. O formato é texto por padrão.
- **API NestJS**: somente para **stdout**, pelo `Logger` do NestJS, em texto.
- **Compose**: cada container usa o driver `json-file` com `max-size: 10m` e `max-file: 3`. Ou seja, até 30 MB por serviço.

### 3.2 Formato JSON no converter

`LOG_JSON` é lido de `os.getenv` em [logging_setup.py](../services/converter/logging_setup.py). Por isso precisa estar **no ambiente do processo**. Um `LOG_JSON=1` escrito apenas no `.env` é ignorado.

```powershell
$env:LOG_JSON = "1"
$env:LOG_LEVEL = "DEBUG"   # opcional
cd services\converter
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

`LOG_LEVEL` é lido das configurações do converter (que incluem o `.env`), então também funciona via arquivo. No compose, o converter já sobe com `LOG_JSON=1`.

### 3.3 Guardando o log em arquivo (Windows)

O `starter-server.cmd` esconde a saída numa janela minimizada. Para gravar em arquivo, rode o uvicorn manualmente:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\pdf2x-logs" | Out-Null
cd services\converter
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --log-level info *>&1 |
  Tee-Object -FilePath "$env:USERPROFILE\pdf2x-logs\converter.log" -Append
```

Em `cmd`:

```bat
cd services\converter
.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 >> "%USERPROFILE%\pdf2x-logs\converter.log" 2>&1
```

Mantenha o log **fora** do repositório, como acima. Arquivos `*.log` são ignorados pelo Git, mas continuam sendo dados pessoais de conversão quando contêm nomes de arquivo.

### 3.4 Logs no compose

```bash
cd infra
docker compose logs -f converter
docker compose logs -f api
docker compose logs --since 30m worker
```

### 3.5 Privacidade nos logs

- A API NestJS registra uma linha por requisição e **redige** `access_token`, `device_id`, `ticket`, `token`, `api_key`, `secret` e `password` nas URLs ([logging.interceptor.ts](../apps/api/src/common/interceptors/logging.interceptor.ts)).
- O converter roda uvicorn com `--log-level info`, e o log de acesso do uvicorn **imprime a query string completa**. Rotas SSE aceitam `?access_token=` ([public_routes.py](../services/converter/api/public_routes.py)). Quando a SPA usa o fallback de `EventSource`, o token aparece no stdout e, se você redirecionou, no arquivo de log. Trate o log do converter como sensível.

---

## 4. Backup

Faça backup com o sistema **parado**, a menos que o comando indique o contrário. O SQLite em modo WAL mantém alterações recentes no arquivo `-wal`: copiar só o `.db` pode gerar um banco inconsistente.

### 4.1 Standalone (converter)

Passos:

1. Pare o servidor (feche a janela ou `Ctrl+C`).
2. Copie **toda** a pasta `services/converter/data/`. Ela contém o banco (`converter.db`, `-wal`, `-shm`), `storage/` e, se existir, `work/`.

```powershell
$dest = "<pasta-de-backup>\pdf2x-$(Get-Date -Format yyyyMMdd-HHmm)"
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item -Recurse -Force "services\converter\data" "$dest\converter-data"
```

```bash
dest="$HOME/backups/pdf2x-$(date +%Y%m%d-%H%M)"
mkdir -p "$dest"
cp -a services/converter/data "$dest/converter-data"
```

Não é necessário copiar `work/`. Os arquivos ali são temporários.

**Backup com o servidor rodando** (para o banco, sem parar o sistema): use a API de backup do SQLite. Ela copia de forma consistente, sem tocar nos dados.

```powershell
python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" "services/converter/data/converter.db" "$dest\converter-live.db"
```

Esse backup cobre só o banco. Os objetos em `storage/` ainda precisam ser copiados, e o estado pode ficar ligeiramente diferente entre as duas cópias. Para backup completo, pare o servidor.

### 4.2 Modo full: arquivos adicionais

Além de `services/converter/data/`, copie também:

- `apps/api/prisma/data/api.db` (banco da API, SQLite; inclua qualquer `-wal` e `-shm` ao lado).
- `apps/api/data/storage/` (documentos e uploads da API, se o backend for local).

```powershell
Copy-Item -Recurse -Force "apps\api\prisma\data" "$dest\api-prisma-data"
Copy-Item -Recurse -Force "apps\api\data" "$dest\api-data"
```

Se a API foi iniciada a partir de outro diretório, o `storage` fica em `<cwd>/data/storage`. Veja [STORAGE.md](STORAGE.md), seção 3.2.

### 4.3 Compose

O compose guarda os dados em volumes. Há duas abordagens.

**A. Dump lógico (recomendado para Postgres):** o banco continua ativo.

```bash
cd infra
docker compose exec -T postgres pg_dump -U pdf2x -d pdf2x_api  > pdf2x_api.sql
docker compose exec -T postgres pg_dump -U pdf2x -d pdf2x_conv > pdf2x_conv.sql
```

No Windows PowerShell, `>` pode gravar em codificação incompatível. Use `cmd /c`:

```powershell
cd infra
cmd /c "docker compose exec -T postgres pg_dump -U pdf2x -d pdf2x_conv > pdf2x_conv.sql"
```

**B. Cópia do volume inteiro:** pare os serviços que escrevem antes, porque copiar o diretório de dados do Postgres com o banco ativo não é consistente.

```bash
cd infra
docker compose stop api converter worker web postgres minio redis

docker run --rm -v pdf2x_pgdata:/v -v "$PWD":/b alpine tar czf /b/pgdata.tgz -C /v .
docker run --rm -v pdf2x_miniodata:/v -v "$PWD":/b alpine tar czf /b/miniodata.tgz -C /v .
docker run --rm -v pdf2x_redisdata:/v -v "$PWD":/b alpine tar czf /b/redisdata.tgz -C /v .

docker compose start postgres minio redis converter worker api web
```

No PowerShell, troque `"$PWD"` por `"${PWD}"`.

O prefixo `pdf2x_` vem de `name: pdf2x` no [docker-compose.yml](../infra/docker-compose.yml). Se você mudar o nome do projeto, o prefixo muda junto. Confira com `docker volume ls --filter name=pdf2x`.

O bucket do MinIO (`miniodata`) guarda os documentos e saídas. Ele precisa ser copiado junto com o Postgres, senão o banco aponta para chaves que não existem mais.

---

## 5. Limpeza e reset

### 5.1 O que é seguro apagar

| Item | Seguro? | Consequência |
|---|---|---|
| `services/converter/data/work/*` com o servidor parado | Sim | Nenhuma. São cópias temporárias. Só apague com nenhum run em execução. |
| `uploads/<id>/` de sessões `completed` (converter) | Sim, com o servidor parado | O documento já foi montado em `documents/`. O item fica como concluído. Reenvios desse upload retornam "já concluído". |
| `uploads/<id>/` de sessões expiradas e não concluídas | Sim | Esses uploads não podem mais receber partes (`410`). O usuário precisa reenviar. |
| `runs/<id>/pages/` (checkpoints) | Não recomendado | Uma reexecução do mesmo run perde o reaproveitamento por página e recomeça do zero. |
| `documents/` | **Não** | Conversões que usam o arquivo deixam de poder ser reprocessadas e exportadas. |
| `converter.db` | **Não**, a não ser em reset completo | Perde o histórico, as conversões e o vínculo com o dono. |

Não existe comando de limpeza automática. Para verificar o que é órfão, use consultas somente leitura.

Para listar sessões de upload concluídas no converter:

```powershell
python -c "import sqlite3; c=sqlite3.connect('services/converter/data/converter.db'); [print(r) for r in c.execute(\"select id, state, expires_at from public_upload_sessions where state in ('completed','aborted')\")]"
```

Para cada `id` retornado, a pasta `services/converter/data/storage/uploads/<id>/` pode ser apagada depois de parar o servidor. Partes de sessões `aborted` já são removidas pelo próprio converter, então qualquer sobra ali também pode ir.

**[DESTRUTIVO]** Apagar uma pasta de upload com o servidor parado:

```powershell
Remove-Item -Recurse -Force "services\converter\data\storage\uploads\<upload_id>"
```

No modo full, a mesma ideia vale para `apps/api/data/storage/uploads/<uploadId>/` e a tabela `UploadSession` do Prisma. Por segurança, ao apagar partes na API, use `sqlite3` ou o Prisma Studio (`npx prisma studio --schema prisma/schema.prisma`, dentro de `apps/api`) para confirmar o `state`.

### 5.2 Reset completo do standalone

**[DESTRUTIVO]** Apaga todas as conversões, o histórico, todos os documentos e o banco.

1. Pare o servidor.
2. Apague a pasta `services/converter/data/` inteira (inclui `converter.db`, `-wal`, `-shm`, `storage/` e `work/`).
3. Inicie o servidor de novo. `get_settings()` recria `data/` e `storage/`, e `init_db()` cria o banco vazio.

```powershell
Remove-Item -Recurse -Force "services\converter\data"
```

Consequências: o dono anônimo atual deixa de ter dados. Se o navegador ainda tiver a identidade, ela continua válida, só que sem conversões.

### 5.3 Reset completo da API (modo full)

**[DESTRUTIVO]** Apaga usuários, conversões e documentos da API.

```powershell
Remove-Item -Recurse -Force "apps\api\prisma\data"
Remove-Item -Recurse -Force "apps\api\data"
npm --prefix apps\api run db:push
```

Para Postgres, o reset equivale a derrubar o banco `pdf2x_api` (por exemplo, `DROP DATABASE` e recriação pelo `init.sql`) e rodar `db:push` de novo.

### 5.4 Resetar o navegador

Remova o que a aplicação guarda. Você pode usar o console do navegador (F12) na página da aplicação:

```javascript
localStorage.removeItem('pdf2x.device_id');
localStorage.removeItem('pdf2x.auth');
indexedDB.deleteDatabase('pdf2x-uploads');
```

Cookies `pdf2x_device` e `pdf2x_device_id` são removidos em **Application > Cookies** do DevTools. O `pdf2x_device_id` é HttpOnly e não aparece no `document.cookie`, então o DevTools é a única forma. O mais simples é usar **Limpar dados do site** para `localhost` ou `127.0.0.1`.

Consequência: o navegador vira um dono novo. As conversões antigas continuam no disco e deixam de aparecer na interface. Veja [STORAGE.md](STORAGE.md), seção 9.

### 5.5 Resetar o MCP

**[DESTRUTIVO]** Apaga a identidade do adaptador MCP e o histórico de envios (`submissions.jsonl`).

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.pdf2x"
```

Se `PDF2X_CONFIG_DIR` estiver definido, apague esse diretório. Consequência: um novo identificador de dispositivo é gerado no próximo uso, e o histórico de envios some. Os arquivos já convertidos continuam no converter, vinculados ao dono antigo.

### 5.6 Compose

| Comando | O que faz | Dados |
|---|---|---|
| `docker compose down` | para e remove containers e rede | preservados |
| **[DESTRUTIVO]** `docker compose down -v` | remove containers, rede **e volumes** | **apaga** `pdf2x_pgdata`, `pdf2x_miniodata` e `pdf2x_redisdata` |

O `down -v` apaga bancos (`pdf2x_api`, `pdf2x_conv`), o bucket e a fila. Faça backup (seção 4.3) antes.

Para apagar um único volume, com o stack parado:

```bash
docker volume rm pdf2x_redisdata
```

Redis é descartável: apagar `pdf2x_redisdata` só descarta jobs pendentes na fila.

---

## 6. Migrações

### 6.1 Converter

- **Standalone**: automático. `init_db()` roda `create_all` e aplica colunas aditivas na subida. Não é preciso comando.
- **Alembic** (opcional, recomendado para Postgres e para versionar mudanças): instale o extra `migrate` e execute a partir de `services/converter`.

```powershell
cd services\converter
.\.venv\Scripts\pip install -e ".[migrate]"
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic upgrade head
```

```bash
cd services/converter
.venv/Scripts/pip install -e ".[migrate]"
.venv/Scripts/python.exe -m alembic current
.venv/Scripts/python.exe -m alembic upgrade head
```

A URL do banco vem de `CONVERTER_DATABASE_URL` pelo [migrations/env.py](../services/converter/migrations/env.py), não do `alembic.ini`. Faça backup antes de migrar um banco que já tem dados.

- **Compose**: o Postgres do converter é criado pelo `init.sql`, e as tabelas pelo `init_db()` na subida do container.

### 6.2 API NestJS

- SQLite (padrão): `npm --prefix apps/api run db:push` aplica o [schema.prisma](../apps/api/prisma/schema.prisma) ao `API_DATABASE_URL`.
- PostgreSQL local: aponte para o schema do Postgres e defina `API_POSTGRES_URL`:

```powershell
cd apps\api
npx prisma db push --schema prisma/schema.postgres.prisma
```

- `npm --prefix apps/api run db:generate` regenera o cliente Prisma. Rode após qualquer alteração de schema.
- Compose: o [docker-entrypoint.sh](../apps/api/docker-entrypoint.sh) executa `prisma db push` a cada boot quando `DATABASE_PROVIDER=postgres`. Para ambientes controlados, troque por `prisma migrate deploy` e uma pasta de migrações versionada.

> O `db push` é idempotente, mas pode alterar tabelas com perda de dados em mudanças destrutivas. Faça backup antes de atualizar o schema.

---

## 7. Atualizar o projeto

```powershell
git pull
npm run install:all
cd services\converter
.\.venv\Scripts\pip install -e ".[full]"
cd ..\..
npm --prefix apps\web run build
npm --prefix apps\api run db:generate
npm --prefix apps\api run db:push
```

Ordem: atualize o código, instale dependências, recompile a interface (o converter serve `apps/web/dist`), gere o cliente Prisma, aplique migrações e só então suba o servidor.

Para o compose:

```bash
git pull
cd infra
docker compose up --build -d
```

Faça backup antes de atualizar. Se a versão nova alterar o schema, a migração roda sozinha no boot (API, no compose) ou no `init_db()` (converter). Não há como desfazer um `db push` destrutivo sem backup.

---

## 8. Solução de problemas

| Sintoma | Causa provável | Como resolver |
|---|---|---|
| `[ERRO] A porta 8000 esta em uso` ao rodar o starter | outro processo na porta 8000 | Descubra o PID com `netstat -ano \| findstr :8000` e encerre com `taskkill /PID <pid> /F`, ou rode em outra porta com `--port` no uvicorn manual |
| A raiz `http://127.0.0.1:8000/` mostra JSON ou 404 | `apps/web/dist` não foi gerado | `npm --prefix apps\web run build` e recarregue a página |
| Dev server do Vite (porta 5173) chama `localhost:3000` em modo standalone | o proxy do Vite aponta para a API NestJS por padrão | defina `VITE_API_PROXY_TARGET=http://localhost:8000` no ambiente (ou em `apps/web/.env.local`) e reinicie o `vite` |
| `OCR_UNAVAILABLE` nas auditorias ou páginas digitalizadas sem texto | Tesseract não encontrado e GLM-OCR indisponível | Instale o Tesseract em `C:\Program Files\Tesseract-OCR\tesseract.exe` (o motor detecta esse caminho no Windows, mas só se `pytesseract` estiver instalado) ou defina `TESSERACT_CMD` como **variável de ambiente real** (não só no `.env`). Alternativa: `ollama serve` e `ollama pull glm-ocr:latest`. |
| `/api/v1/health/ready` com `ocr_available: false` | o mesmo do item anterior | veja o item anterior |
| `401` em chamadas depois de muito tempo | token JWT expirado | A aplicação troca o token expirado por uma sessão anônima nova na próxima chamada. Se persistir, limpe `pdf2x.auth` (seção 5.4). |
| `FILE_TOO_LARGE` com documento acima de 50 MiB | o envio legado de arquivo único (`POST /api/v1/conversions`) tem teto de 50 MiB (`UPLOAD_MAX_BYTES` e `CONVERTER_MAX_PDF_BYTES`) | Se o erro vier desse endpoint, use o envio por partes (`/api/v1/uploads`), que não impõe teto ao tamanho do documento (`max_file_bytes` é `null` em `/api/v1/capabilities`). Cada parte é limitada por `UPLOAD_PART_BYTES` (converter) e `UPLOAD_PART_MAX_BYTES` (NestJS). |
| Conversão fica em `queued` no modo full | o converter está fora, ou `CONVERTER_INTERNAL_TOKEN` difere entre a API e o converter | Teste `/internal/v1/health` com o token. O outbox da API tenta de novo a cada 5 segundos. |
| `QUEUE_DELIVERY_FAILED` | o outbox desistiu depois de 8 tentativas | Corrija a causa (converter fora ou token divergente) e reenvie a conversão pela interface ou pela API |
| Disco cresce sem parar | partes de upload não são removidas após `complete`, e nada apaga por idade | Veja [STORAGE.md](STORAGE.md), seções 6 e 11, e a seção 5.1 deste guia |
| Assistente desativado | `ASSISTANT_ENABLED=false` (padrão) | Defina `ASSISTANT_ENABLED=true`, rode `ollama pull qwen3.5:4b` e `ollama pull nomic-embed-text`, e confira `/api/v1/capabilities` |
| Container `api` aparece como `unhealthy` no compose | o healthcheck da imagem consulta `http://127.0.0.1:3000/health`, alias na raiz registrado em `apps/api/src/main.ts`. Se o relatório não puder ser montado, a rota responde `503`. | Confirme com `docker compose exec api wget -qO- http://127.0.0.1:3000/health` e leia o campo `services` e o log do contêiner. |
| Health `degraded` com `storage: down` | o diretório de armazenamento não existe ou não tem permissão | Verifique o caminho resolvido (veja [STORAGE.md](STORAGE.md), seções 2.1 e 3.2) e as permissões da pasta |

---

## 9. Segurança local

- **Segredos padrão**: `CONVERTER_INTERNAL_TOKEN` e `JWT_SECRET` têm valores de desenvolvimento públicos no código (`dev-internal-token-change-me` e `dev-jwt-secret-change-me` no converter; `dev-only-insecure-secret-change-me` no NestJS para o JWT). O converter registra um aviso no log de startup quando eles estão em uso. Troque antes de usar a máquina com outras pessoas.
- **Binds**: mantenha `--host 127.0.0.1`. A API NestJS usa `API_HOST` com padrão `127.0.0.1`. O MCP em HTTP escuta em `127.0.0.1` por padrão. O compose publica apenas `web` em `127.0.0.1:${WEB_PORT:-8080}`, e os demais containers não publicam portas para o host.
- **Não exponha portas**: não publique `0.0.0.0` nem faça redirecionamento de porta no roteador ou em túneis. O projeto não tem autenticação forte para uso público.
- **Token em URL**: rotas SSE aceitam `access_token` na query string. Ele pode aparecer nos logs do uvicorn (seção 3.5). Não compartilhe logs brutos.
- **Credenciais do MinIO e do Postgres** no compose usam valores padrão (`minioadmin`, `pdf2x`). Altere `POSTGRES_PASSWORD`, `S3_ACCESS_KEY` e `S3_SECRET_KEY` no `.env` se outras pessoas tiverem acesso à máquina.
- **MCP**: o arquivo `identity.json` é gravado com modo `0600`, mas essa permissão não tem efeito no Windows. Trate `~/.pdf2x/` como dado pessoal.
- **Dados pessoais**: documentos e saídas ficam em disco sem criptografia. Use criptografia de disco do sistema operacional (BitLocker ou equivalente) se o computador for compartilhado.
