# Publicação (deploy)

Este guia descreve a publicação recomendada do Conversor Generator:

- **Frontend (`apps/web`)**: Vercel, como SPA estática. O `vercel.json` na raiz do
  repositório define build, saída e reescritas.
- **API (`apps/api`) e motor (`services/converter`)**: um host de containers de
  longa duração (uma VM com Docker, ou uma plataforma de containers), usando os
  Dockerfiles do repositório, com PostgreSQL e armazenamento persistente.

Os comandos e variáveis citados existem no repositório. Onde o comportamento
depende da plataforma escolhida, o texto diz isso explicitamente.

> Ordem sugerida: primeiro o backend (para obter uma URL HTTPS), depois a Vercel
> (para usar essa URL em `VITE_API_BASE`), e por fim o backend de novo para
> liberar a origem da Vercel em `ALLOWED_ORIGINS`.

---

## Arquitetura de produção

```
navegador ──HTTPS──► Vercel (SPA estática: apps/web/dist)
    │
    └──HTTPS (CORS: ALLOWED_ORIGINS)──► API NestJS (apps/api, :3000 interno)
                                            │  x-internal-token (rede privada)
                                            ▼
                                       Motor FastAPI (services/converter, :8000 interno)
                                            │
                       PostgreSQL ◄─────────┴────────► armazenamento (volume ou S3/MinIO)
                       (pdf2x_api, pdf2x_conv)
                                            │
                                  Tesseract (na imagem) / Ollama (opcional, GPU)
```

Somente a API precisa estar exposta à internet. O motor deve ficar em rede
privada, acessível apenas pela API.

---

## Pré-requisitos

- Repositório no GitHub (veja o README, seção "Publicando no GitHub").
- Conta na Vercel com acesso ao repositório.
- Um host de containers com Docker, ou uma plataforma que construa imagens a
  partir de Dockerfile.
- Um domínio ou subdomínio com HTTPS para a API (por exemplo,
  `https://api.seu-dominio.exemplo`). O navegador bloqueia chamadas HTTP a partir
  de uma página HTTPS.
- Segredos gerados com `openssl rand -hex 32` (um para `JWT_SECRET`, outro para
  `CONVERTER_INTERNAL_TOKEN`).

---

## Parte A: Frontend na Vercel

O `vercel.json` na raiz define o comando de instalação, o comando de build, o
diretório de saída (`apps/web/dist`) e uma reescrita para SPA em todas as rotas,
exceto `/api/`. Não é necessário sobrescrever essas configurações no painel.

1. Na Vercel, selecione **Add New... → Project** e importe o repositório do GitHub.
2. **Root Directory**: mantenha a **raiz do repositório** (não escolha
   `apps/web`). O build depende de `packages/contracts`, que fica fora de
   `apps/web`.
3. **Framework Preset**: **Other**. O `vercel.json` é lido automaticamente.
4. **Node.js Version**: escolha **22** nas configurações do projeto, para
   corresponder ao `.nvmrc` na raiz e às imagens Docker.
5. Em **Environment Variables**, adicione:

   | Nome | Valor | Ambientes |
   | --- | --- | --- |
   | `VITE_API_BASE` | `https://api.seu-dominio.exemplo/api/v1` | Production (e Preview, se quiser testar previews) |

   `VITE_API_BASE` é lida **em tempo de build**: o valor entra no bundle
   JavaScript. Depois de alterá-la, faça um novo deploy (**Redeploy**). Mudar a
   variável sem redeploy não tem efeito.
6. Clique em **Deploy**.

### Se `VITE_API_BASE` não estiver definida

A interface usa `/api/v1` como padrão (`apps/web/src/lib/api.ts`). No domínio da
Vercel esse caminho não existe no frontend, e a reescrita não encaminha `/api/`.
Por isso, todas as chamadas à API falham. Defina a variável antes do primeiro
deploy de produção.

### Previews

Cada deploy de preview tem um domínio próprio. Para que um preview consiga
chamar a API, a origem exata desse domínio precisa estar em `ALLOWED_ORIGINS`.
O CORS compara a origem por igualdade, sem curingas. Para testes de longo prazo,
use apenas o domínio de produção.

### Domínio próprio

Se você adicionar um domínio personalizado à Vercel, inclua a origem nova (com
`https://`, sem barra final) em `ALLOWED_ORIGINS` no backend e reinicie-o.

---

## Parte B: Backend em host de containers

### Opções

| Opção | Quando usar | Observações |
| --- | --- | --- |
| **B1. VM com Docker e `infra/docker-compose.yml`** | stack completa, com PostgreSQL, Redis, MinIO e worker | o compose não publica a porta da API; é preciso um proxy reverso com HTTPS na VM. Não executado no ambiente de construção. |
| **B2. Plataforma de containers (Render, Railway, Fly.io ou similar)** | evitar administrar servidores | dois serviços (`api` e `converter`), PostgreSQL gerenciado e armazenamento. O `converter` deve ficar em rede privada. Um volume costuma ser anexado a um serviço por vez; nesse caso use S3/MinIO. |
| **B3. Modo `standalone` em um único container** | o menor número de peças | um processo FastAPI em `RUNTIME_MODE=standalone` expõe `/api/v1` com SQLite, disco local e fila `inline`. Não há NestJS nem PostgreSQL. A API pública é a mesma (`docs/API.md`). Adequado para uso individual ou carga baixa. |

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

### Variáveis de ambiente do backend

Defina estas variáveis no provedor (ou em `infra/.env`, no caso do compose). Não
faça commit de valores reais.

**Essenciais em produção**

| Variável | Serviço | Valor |
| --- | --- | --- |
| `RUNTIME_MODE` | ambos | `full` (B1/B2) ou `standalone` (B3) |
| `JWT_SECRET` | api (e motor em standalone) | segredo aleatório, 64 caracteres hexadecimais |
| `CONVERTER_INTERNAL_TOKEN` | api e motor | **o mesmo valor** nos dois serviços |
| `ALLOWED_ORIGINS` | api e motor | `https://<projeto>.vercel.app` (ou seu domínio), separados por vírgula, sem barra final |
| `PUBLIC_API_URL` | api | `https://api.seu-dominio.exemplo` (exposto na configuração; no código atual não há uso funcional confirmado) |
| `CONVERTER_INTERNAL_URL` | api | URL do motor na rede privada, por exemplo `http://converter:8000` (modo `full`) |
| `API_POSTGRES_URL` | api | `postgresql://usuario:senha@host:5432/pdf2x_api?schema=public` |
| `CONVERTER_DATABASE_URL` | motor | `postgresql+psycopg://usuario:senha@host:5432/pdf2x_conv` |
| `DATABASE_PROVIDER` | api | `postgres` (com o mesmo build arg na imagem) |
| `STORAGE_BACKEND` | api e motor | `s3` (recomendado em B2 e B1) ou `local` (ver abaixo) |

**Armazenamento**

- Com `STORAGE_BACKEND=s3`, configure `S3_ENDPOINT`, `S3_BUCKET`,
  `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` e `S3_SECURE`. Para MinIO, o
  compose define `S3_FORCE_PATH_STYLE=true` no serviço `api`.
- Com `STORAGE_BACKEND=local`, a API **e o motor** leem e gravam o mesmo
  diretório. O motor recebe a chave de armazenamento da API e lê o PDF dela. Por
  isso, os dois serviços precisam compartilhar o mesmo volume, no mesmo caminho
  (`STORAGE_LOCAL_ROOT`). Em dois serviços separados, isso normalmente exige S3.
  A opção local é mais simples apenas no modo B3.

**Motor e fila**

| Variável | Recomendação |
| --- | --- |
| `CONVERTER_QUEUE` | `inline` (padrão, threads no próprio processo) ou `celery` (requer Redis e um worker separado, como no compose) |
| `CONVERTER_WORKERS` | concorrência do pool `inline` |
| `CONVERTER_TASK_TIMEOUT_SECONDS` | `600` (padrão) |
| `CONVERTER_MAX_PAGES`, `CONVERTER_MAX_PDF_BYTES` | valores de produção conforme o seu hardware |

**Limites e privacidade**

| Variável | Padrão | Observação |
| --- | --- | --- |
| `UPLOAD_MAX_BYTES` | `52428800` (50 MiB) | aplicado pela API e pelo motor. O proxy ou o provedor pode impor limite menor: confira |
| `UPLOAD_MAX_PAGES` | `500` | |
| `AUTH_MODE` | `optional` | sessão por navegador, sem login. `required` aceita apenas JWT emitido por um emissor confiável; não há tela de login |
| `RETENTION_*_DAYS` | `7` / `30` / `90` | informados em `/settings`. Não encontrei uma rotina agendada de expurgo no código; veja o item 10 do checklist pós-deploy |
| `LOG_LEVEL`, `LOG_JSON` | `INFO`, `1` nas imagens | |

**OCR e assistente**

- **Tesseract** já vem instalado na imagem do motor (`tesseract-ocr` e
  `tesseract-ocr-por`). Não é preciso configurar nada para ele.
- **GLM-OCR** depende do Ollama. Dentro de um container, `GLMOCR_HOST=127.0.0.1`
  aponta para o próprio container, e não para a máquina host. Para usá-lo, defina
  `GLMOCR_HOST` e `GLMOCR_PORT` com um endereço alcançável, ou defina
  `GLMOCR_ENABLED=false`. Sem Ollama, páginas digitalizadas recorrem ao Tesseract
  ou falham de forma explícita.
- **Ollama com GPU** exige um host com placa compatível. Plataformas de
  containers comuns não oferecem GPU. Esse cenário não foi testado neste
  repositório.
- **Assistente** (`ASSISTANT_ENABLED`) vem desativado por padrão. Ative-o apenas
  com um Ollama acessível e o modelo `qwen3.5:4b` instalado.
- **Privacidade do OCR:** o motor fala com o Ollama diretamente, sem o cliente
  MaaS do SDK `glmocr`, que envia documentos à nuvem quando há `ZHIPU_API_KEY` no
  ambiente. Mantenha `GLMOCR_HOST` em loopback ou em rede privada e não defina
  `ZHIPU_API_KEY` neste deploy.

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

### Rotação de segredos

- **`CONVERTER_INTERNAL_TOKEN`**: troque nos dois serviços (API e motor) no mesmo
  intervalo e reinicie ambos. Se a troca for feita em apenas um, as chamadas
  internas falham com `401`.
- **`JWT_SECRET`**: tokens já emitidos deixam de ser válidos. Os clientes obtêm
  uma nova sessão pelo fluxo de sessão local. Confirme isso no teste de fumaça
  após a troca.
- Não coloque segredos no `vercel.json`, no código ou em logs. As variáveis
  `VITE_*` são públicas, porque entram no bundle do navegador. Não coloque nelas
  nenhum segredo.

### HTTPS, origem e cookies

- A API deve responder em HTTPS. A Vercel serve o frontend em HTTPS, e o
  navegador bloqueia conteúdo misto.
- O CORS é configurado pela lista `ALLOWED_ORIGINS`, comparada exatamente. Use
  `https://<projeto>.vercel.app`, sem barra final e sem caminho.
- Frontend e backend ficam em sites diferentes. Por isso, o cookie
  `pdf2x_device_id` (SameSite=Lax) **não** é enviado nas requisições do frontend.
  A identidade do navegador passa pelo cabeçalho `x-device-id`, que o frontend
  envia com o valor guardado no `localStorage` do domínio da Vercel. Limpar o
  `localStorage` cria um novo proprietário, com workspace vazio. Essa é a
  consequência da arquitetura separada, e não um erro de configuração.

### Persistência

| Dado | Onde fica | O que montar |
| --- | --- | --- |
| Banco da API | PostgreSQL (`pdf2x_api`) ou SQLite | banco gerenciado, com backup |
| Banco do motor | PostgreSQL (`pdf2x_conv`) ou SQLite | banco gerenciado, com backup |
| PDFs e XLSX | S3/MinIO **ou** disco local | bucket com backup, ou volume persistente |

- Nas imagens, os caminhos padrão são relativos a `/app` (`./data/...`). Defina
  caminhos absolutos no provedor (por exemplo, `STORAGE_LOCAL_ROOT=/data/storage`
  e `CONVERTER_DATABASE_URL=sqlite:////data/converter.db`) e monte o volume nesse
  caminho. A imagem do motor cria `/data/storage`, mas o padrão do código não
  aponta para ele.
- **SQLite na imagem da API**: o `docker-entrypoint.sh` aplica o schema apenas
  para PostgreSQL. Com SQLite, as tabelas precisam existir antes do primeiro uso
  (por exemplo, com `npx prisma db push` executado no container). Por isso, em
  containers recomendamos PostgreSQL.
- As tabelas do motor são criadas na inicialização (`init_db`). Migrações Alembic
  (`alembic upgrade head`) são a alternativa para ambientes controlados.

### Infra do compose (B1)

- O compose sobe `postgres:16-alpine`, `redis:7-alpine`, `minio`, `converter`,
  `worker` (Celery), `api` e `web`. O serviço `web` é desnecessário quando a
  interface está na Vercel: pode ser omitido da execução.
- Somente o `web` publica uma porta (`WEB_PORT`, padrão `8080`). Para a API, é
  preciso incluir `ports` ou um proxy reverso com HTTPS na VM (Caddy, Traefik ou
  nginx), encaminhando para o serviço `api` na porta `3000`.
- Troque as credenciais padrão do MinIO (`minioadmin`) e do PostgreSQL (`pdf2x`)
  antes de expor o host.

---

## Parte C: Por que o motor não roda em funções serverless (Vercel)

O motor não é adequado a funções serverless, por motivos verificáveis no código:

- Mantém threads de trabalho de longa duração (`CONVERTER_QUEUE=inline`). Uma
  conversão pode durar até `CONVERTER_TASK_TIMEOUT_SECONDS` (600 s por padrão).
  Funções serverless encerram ao fim da requisição ou ao atingir o seu próprio
  tempo limite.
- Usa binários nativos: Tesseract, pdfium (`pypdfium2`), OpenCV. Eles precisam de
  um sistema de arquivos e de bibliotecas do sistema completas.
- Grava em disco (SQLite, storage local, arquivos temporários da normalização de
  `/Rotate`). Um disco de função não persiste entre invocações.
- A API mantém processos de fundo: o drenador da outbox roda a cada 5 segundos
  (`DRAIN_INTERVAL_MS`) e o SSE mantém conexões abertas por longos períodos.
- Uploads de até 50 MiB (`UPLOAD_MAX_BYTES`) passam pelo corpo da requisição.
  Funções serverless têm limites de corpo de requisição menores que esse valor
  (consulte a documentação atual da Vercel).

Por isso, a Vercel hospeda apenas o frontend estático. O upload vai do navegador
diretamente para a API.

---

## Limitações conhecidas

- **Fallback de SSE por `EventSource`**: o caminho principal de progresso usa
  `fetch` com streaming, e envia `x-device-id` e `Cache-Control` no cabeçalho.
  Esse caminho funciona entre origens porque a API libera `Cache-Control` no
  CORS. O fallback para `EventSource`
  (`apps/web/src/hooks/use-conversion-events.ts`) não consegue enviar
  `x-device-id` em requisição cross-origin. Ele só é usado quando o
  `fetch` falha; nesse caso o cabeçalho de identidade não é enviado e o
  fallback deve ser tratado como degradado.
- **`ALLOWED_ORIGINS` do motor**: o motor Python também lê `ALLOWED_ORIGINS`.
  Inclua a origem da Vercel nele, além da API, para que o navegador possa chamar
  o motor diretamente, se isso for usado.

## Checklist pós-deploy

Execute na ordem:

1. **Saúde da API**: `GET https://api.seu-dominio.exemplo/health` responde `200`
   com `"status": "ok"` e todos os serviços `up`. Se algum estiver `down`,
   verifique a conexão com o banco, o storage e o motor.
2. **Motor interno**: a porta do motor não deve estar publicada na internet.
   Confirme no provedor (ou no `ports:` do compose) que apenas a API está exposta.
   As rotas `/internal/v1/*` exigem `x-internal-token`.
3. **Origem liberada**: a Vercel carrega a página sem erros de CORS no console
   do navegador. Se houver erro, confira `ALLOWED_ORIGINS`.
4. **Frontend apontando para a API certa**: a aba de rede do navegador mostra
   chamadas para `https://api.seu-dominio.exemplo/api/v1/...`, e não para
   `/api/v1` no domínio da Vercel.
5. **Teste de fumaça**: envie um PDF pequeno com texto nativo. Acompanhe o
   progresso por SSE, baixe o XLSX e abra a auditoria.
6. **Teste de digitalizado** (se houver OCR configurado): envie uma página sem
   camada de texto e confirme que o resultado é recuperado ou que a falha é
   explícita.
7. **Logs**: o motor não registra avisos de segredo com valor padrão ao iniciar.
   Se o aviso aparecer, troque o segredo indicado. Os logs não devem conter
   conteúdo de células (verifique em uma conversão de teste).
8. **Exposição**: apenas a API (e o frontend) estão acessíveis. PostgreSQL, Redis,
   MinIO e o motor ficam em rede privada.
9. **Backups**: o PostgreSQL e o bucket S3 (ou o volume) têm cópias de segurança
   configuradas e testadas.
10. **Retenção**: confirme como os prazos de `RETENTION_*` serão aplicados. O
    código informa os prazos, mas não encontrei uma rotina agendada de expurgo.
    Defina a rotina operacional antes de tratar dados reais. A base legal (LGPD) e
    os prazos cabem ao operador, conforme o tipo de documento; `DELETE
    /conversions/:id` faz exclusão lógica e remove os artefatos do storage.
11. **Limites**: confirme que o limite do proxy ou do provedor aceita o
    `UPLOAD_MAX_BYTES` configurado.
12. **Preview**: se usar previews da Vercel, confirme que a origem de cada um
    está em `ALLOWED_ORIGINS`.
