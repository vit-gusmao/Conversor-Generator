# Motor de conversão (Python)

Serviço FastAPI que executa o pipeline PDF → XLSX e persiste toda a
proveniência.

> **Somente local.** O launcher (`starter-local-server.cmd`) sobe o motor em
> `127.0.0.1:8000`. Dentro do contêiner o uvicorn usa `0.0.0.0`, mas isso vale só
> para o contêiner. Os dados ficam em `services/converter/data/` (banco SQLite
> `converter.db`, `storage/` e `work/`). Veja [`../../docs/STORAGE.md`](../../docs/STORAGE.md).

Roda em dois modos:

| Modo | Comando | Quem chama |
| --- | --- | --- |
| `standalone` | `uvicorn api.main:app --port 8000` | navegador (também serve o SPA) |
| `full` | mesmo comando | gateway NestJS via `/internal/v1` |

## Instalação

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"        # Linux/macOS: .venv/bin/pip
# extras: .[ocr] .[queue] .[s3] .[postgres] .[docling] .[camelot] .[migrate] .[full]
```

## Execução

```bash
.venv/Scripts/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

`--host 127.0.0.1` é o que o launcher usa. Não exponha o motor em uma interface
pública: as rotas `/internal/v1` são protegidas apenas pelo `CONVERTER_INTERNAL_TOKEN`.

- API pública: `/api/v1` (contrato em `docs/API.md`)
- API interna: `/internal/v1` (token `CONVERTER_INTERNAL_TOKEN`)
- Docs interativas: `/docs`
- Health: `/health` e `/internal/v1/health`
- Interface: se `apps/web/dist/index.html` existir, o motor também serve o SPA em `/` (gerado por `npm run build` em `apps/web`)

## Dados locais

| Caminho (relativo a `services/converter/`) | Conteúdo | Variável |
| --- | --- | --- |
| `data/converter.db` | Banco SQLite (proveniência, runs, lotes, uploads) | `CONVERTER_DATABASE_URL=sqlite:///./data/converter.db` |
| `data/storage/` | PDFs e imagens recebidos (armazenamento local) | `STORAGE_LOCAL_ROOT=./data/storage` |
| `data/work/` | Cópias de trabalho de cada run (`<run_id>.source`, `<run_id>.derotated.pdf`) | fixo, em `data/` |

Caminhos relativos são resolvidos contra o diretório do pacote (`services/converter/`),
não contra o diretório atual do processo. Assim o motor grava no mesmo lugar
qualquer que seja o diretório de onde ele foi iniciado. Ao iniciar, `config.py`
cria `data/` e `data/storage/`. Para o mapa completo de dados, veja
[`../../docs/STORAGE.md`](../../docs/STORAGE.md). A API Node guarda os próprios
arquivos em `apps/api/`, que é outro conjunto de dados.

Configuração completa: [`../../docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md).
Operação (backup, limpeza e logs): [`../../docs/OPERATIONS.md`](../../docs/OPERATIONS.md).
Fluxo de ponta a ponta: [`../../docs/FLOW.md`](../../docs/FLOW.md).

## CLI

```bash
.venv/Scripts/python -m worker.cli documento.pdf -o saida.xlsx
```

Opções: `-o/--output` (caminho de saída) e `--no-ocr`.

## Empacotamento Windows

O backend congelado (PyInstaller, onedir) é gerado por `packaging/converter.spec`.
O fluxo completo, com o aplicativo Electron, está em
[`packaging/README.md`](packaging/README.md) e em [`../../docs/DESKTOP.md`](../../docs/DESKTOP.md).

## Migrações

O Alembic não vem na instalação básica. Instale o extra `migrate` antes:

```bash
.venv/Scripts/pip install -e ".[migrate]"        # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m alembic upgrade head     # Linux/macOS: .venv/bin/python
```

As migrações ficam em `migrations/versions/`, e a URL do banco vem de
`CONVERTER_DATABASE_URL` (não de `alembic.ini`).

Na inicialização, o motor chama `init_db()` em qualquer modo: ele cria as tabelas
ausentes e aplica alterações aditivas. Alembic (`alembic upgrade head`) é a
alternativa versionada para ambientes controlados.

## Testes

```bash
.venv/Scripts/python -m pytest tests -q          # Linux/macOS: .venv/bin/python
.venv/Scripts/python scripts/smoke_run.py        # inspeção manual por fixture
.venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs   # corpus local (não versionado)
```

`stress_gabarito.py` pontua a conversão contra o `GABARITO.jsonl` do corpus de
stress. A pasta `stress/pdfs` não faz parte do repositório; veja
[`../../docs/TESTING.md`](../../docs/TESTING.md#stress-contra-o-corpus-dourado-3208-pdfs).

A quantidade de testes muda com o código; o resumo do pytest é a referência.
Testes que dependem de Ollama ou do GLM-OCR são pulados quando o serviço ou o
modelo não existem.

## Estrutura

```
api/            FastAPI (rotas públicas e internas, SSE, auth)
engine/         pipeline e todos os estágios
audit/          eventos encadeados, cobertura, evidência, manifesto
models/         IR universal e schema SQLAlchemy
repositories/   escrita/leitura da proveniência
storage/        local | S3
jobqueue/       inline | celery
worker/         execução, tarefas Celery, CLI
migrations/     Alembic
tests/          fixtures geradas + suítes
```

## Docker

Construa a partir da raiz do repositório. O contexto precisa ser a raiz porque o
`Dockerfile` copia `services/converter/` com caminhos a partir dela. A imagem não
depende de arquivos fora de `services/converter`: `apps/web/dist` e `study/` não
entram na imagem, e a interface não é servida por ela.

```bash
docker build -f services/converter/Dockerfile -t pdf2x-converter .
```

A imagem (`python:3.12-slim`) instala Tesseract com o pacote de idioma português,
as bibliotecas do OpenCV e os extras `full` e `postgres`. Ela executa como usuário
sem privilégios (uid 10001). Dentro do contêiner, os caminhos padrão ficam em
`/app/data/` (o diretório de trabalho é `/app`). A imagem também cria `/data/storage`,
mas nenhum padrão aponta para ele: para usá-lo, defina `STORAGE_LOCAL_ROOT=/data/storage`
e monte um volume. Para persistência, use caminhos absolutos em um volume. O
GLM-OCR não está na imagem: ele depende de um Ollama alcançável (`GLMOCR_HOST`/`GLMOCR_PORT`).
Dentro do contêiner o uvicorn escuta em `0.0.0.0` (`CMD` do Dockerfile). A exposição
ao host depende apenas do mapeamento de portas do `docker run`. Veja
[`../../docs/DEPLOY.md`](../../docs/DEPLOY.md).

Detalhes do pipeline: [`../../docs/CONVERSION.md`](../../docs/CONVERSION.md).
