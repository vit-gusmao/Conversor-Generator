# Referência de configuração

Esta página lista cada variável de ambiente que o código realmente lê, para a versão 100% local do Conversor Generator. Os padrões, os arquivos que cada componente lê e o status de cada variável foram conferidos no código-fonte em 2026-11-18.

Para o fluxo de uma conversão, veja [FLOW](FLOW.md). Para armazenamento e retenção, veja [STORAGE](STORAGE.md). Para operação no dia a dia, veja [OPERATIONS](OPERATIONS.md).

## Índice

1. [Como a configuração é carregada](#como-a-configuração-é-carregada)
2. [Legenda de status](#legenda-de-status)
3. [Motor de conversão (converter)](#motor-de-conversão-converter)
4. [API pública (NestJS)](#api-pública-nestjs)
5. [Interface web (Vite)](#interface-web-vite)
6. [Adaptador MCP](#adaptador-mcp)
7. [Docker Compose](#docker-compose)
8. [Segredos locais](#segredos-locais)
9. [Opções por conversão](#opções-por-conversão)
10. [Receitas](#receitas)
11. [Pontos de atenção](#pontos-de-atenção)

---

## Como a configuração é carregada

A precedência geral, do mais forte para o mais fraco, é:

1. Variável de ambiente real do processo (`$env:NOME="valor"` no PowerShell, `set NOME=valor` no cmd).
2. Arquivo `.env` lido pelo componente.
3. Padrão definido no código.

Cada componente lê arquivos diferentes. Esta é a parte que mais causa confusão.

### Motor de conversão (Python)

`services/converter/config.py` usa `pydantic-settings`. Ele lê, nesta ordem:

1. `<raiz>/.env`
2. `services/converter/.env`

O último arquivo encontrado vence, então `services/converter/.env` sobrepõe o da raiz. Se nenhum existir, o motor procura `.env` no diretório de execução. Os nomes não diferenciam maiúsculas e minúsculas.

Os valores vão para o objeto `Settings`. Eles **não** são copiados para `os.environ`. Por isso, três variáveis lidas com `os.getenv` ou `os.environ` precisam ser variáveis de ambiente reais do processo, e não entradas de `.env`:

| Variável | Onde é lida | Como definir |
|---|---|---|
| `CONVERTER_OCR_ENGINE` | `engine/ocr/engines.py` | variável de processo |
| `TESSERACT_CMD` | `engine/ocr/engines.py` | variável de processo |
| `LOG_JSON` | `logging_setup.py` | variável de processo |

No PowerShell: `$env:LOG_JSON="1"`. No cmd: `set LOG_JSON=1`. Só vale para a janela onde o comando foi executado.

`LOG_LEVEL` funciona no `.env`, porque o `Settings` é que é passado para `configure_logging`.

### API pública (NestJS)

`apps/api/src/config/app-config.module.ts` usa `@nestjs/config` com `envFilePath: ['../../.env', '.env']`. Esses caminhos são **relativos ao diretório de execução**. Quando a API roda por `npm --prefix apps/api run start:dev`, o diretório é `apps/api`, e os caminhos funcionam. Se você executar `node apps/api/dist/main.js` a partir da raiz, `../../.env` aponta para fora do repositório e o `.env` da raiz não é encontrado.

Quando o mesmo nome aparece nos dois arquivos, o **primeiro** encontrado vence: o da raiz tem precedência sobre `apps/api/.env`.

### Interface web (Vite)

`apps/web/vite.config.ts` chama `loadEnv(mode, process.cwd(), '')`. Com o Vite rodando em `apps/web`, ele lê **`apps/web/.env*`**, e **não** o `.env` da raiz. Portanto `VITE_API_BASE` e `VITE_API_PROXY_TARGET` precisam estar em `apps/web/.env` (ou no ambiente do shell) para terem efeito.

As variáveis `VITE_*` são embutidas no bundle no momento do build. Mudar `VITE_API_BASE` exige um novo `npm run build`.

### Adaptador MCP

`apps/mcp` não usa nenhuma biblioteca de `.env`. Ele lê apenas `process.env`. Defina as variáveis no ambiente do processo ou no bloco `env` da configuração do host MCP (Claude Desktop, Cursor etc.).

### Exemplos de `.env`

**Standalone, uso local.** Não é preciso nenhum `.env`: todos os padrões funcionam. Se quiser ajustar algo, crie `<raiz>/.env` com:

```
RUNTIME_MODE=standalone
CONVERTER_OCR_ENABLED=true
CONVERTER_INTERNAL_TOKEN=troque-por-um-valor-longo
JWT_SECRET=troque-por-outro-valor-longo
```

**Modo full, uso local.** Um único `<raiz>/.env` serve à API NestJS e ao motor, então `JWT_SECRET` e `CONVERTER_INTERNAL_TOKEN` ficam iguais automaticamente:

```
RUNTIME_MODE=full
API_PORT=3000
API_HOST=127.0.0.1
CONVERTER_INTERNAL_URL=http://localhost:8000
CONVERTER_INTERNAL_TOKEN=<valor gerado, ver Segredos locais>
JWT_SECRET=<valor gerado, ver Segredos locais>
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:3000
PUBLIC_API_URL=http://localhost:3000
```

O `PDF2X_API_BASE_URL` do MCP **não** vai nesse arquivo, porque o MCP não lê `.env`. Veja [Adaptador MCP](#adaptador-mcp).

---

## Legenda de status

| Status | Significado |
|---|---|
| **ativo** | Lida e usada no comportamento. |
| **só informativo** | Lida, mas só aparece em respostas de configuração ou status. Nenhuma decisão de execução depende dela. |
| **lido mas sem efeito** | Declarada e parseada, mas nenhum código a consome. |
| **anunciado mas não aplicado** | Aparece em respostas de capacidade ou configuração para a interface, mas nenhum código recusa ou limita nada com ela. |
| **obsoleto** | Mantida só por compatibilidade. O padrão do produto não a aplica. |

---

## Motor de conversão (converter)

Caminhos relativos a `services/converter/`. Os nomes de variável são os aliases do `Settings`. O total é de 74 campos.

### Runtime

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `RUNTIME_MODE` | `standalone` | `config.py`; `api/public_routes.py` | só informativo | Declara o modo do motor: `standalone` ou `full`. Outro valor impede a inicialização. | Nenhum comportamento depende dele. O padrão do NestJS é `full`. |
| `LOG_LEVEL` | `INFO` | `config.py`; `api/main.py`; `worker/cli.py` (via `configure_logging`) | ativo | Nível do log: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Valor inválido cai para `INFO`. | Funciona no `.env`. |
| `LOG_JSON` | vazio (texto) | `logging_setup.py` (`os.getenv`) | ativo, só variável de processo | Logs em JSON quando `1`, `true` ou `yes`. | Não lida do `.env`. |
| `CONVERTER_PORT` | `8000` | `config.py` | só informativo | Porta do motor. | Nenhum código a usa. `starter-server.cmd` fixa `127.0.0.1:8000`, e o uvicorn recebe `--port` na linha de comando. |
| `WEB_ORIGIN` | `http://localhost:5173` | `config.py` | lido mas sem efeito | Origem do SPA. | Sem leitor. A origem liberada no CORS é `ALLOWED_ORIGINS`. |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | `config.py` (campo `allow_origins`); `api/main.py` (CORS) | ativo | Origens liberadas no CORS, separadas por vírgula. | Se for definida como vazia, o CORS passa a aceitar `*`. Não deixe vazia. |

### Armazenamento

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_DATABASE_URL` | `sqlite:///./data/converter.db` | `config.py`; `models/db.py`; `migrations/env.py` | ativo | Banco do motor. Aceita SQLite ou `postgresql+psycopg://...`. | O caminho SQLite é relativo a `services/converter/`, não ao diretório de execução. |
| `STORAGE_BACKEND` | `local` | `config.py`; `storage/__init__.py`; `api/main.py` | ativo | `local` ou `s3`. Outro valor impede a inicialização. | |
| `STORAGE_LOCAL_ROOT` | `./data/storage` | `config.py` (`storage_dir`); `storage/local.py` | ativo | Pasta dos artefatos quando `STORAGE_BACKEND=local`. | Relativa a `services/converter/`. |
| `S3_ENDPOINT` | nenhum | `storage/s3.py` | ativo com `s3` | URL do S3 ou MinIO. | |
| `S3_REGION` | `us-east-1` | `storage/s3.py` | ativo com `s3` | Região do bucket. | |
| `S3_ACCESS_KEY` | nenhum | `storage/s3.py` | ativo com `s3` | Chave de acesso. | Não versionar. |
| `S3_SECRET_KEY` | nenhum | `storage/s3.py` | ativo com `s3` | Segredo de acesso. | Não versionar. |
| `S3_BUCKET` | `pdf2x` | `storage/s3.py` | ativo com `s3` | Nome do bucket. | |
| `S3_SECURE` | `false` | `storage/s3.py` | ativo com `s3` | Usa TLS quando `true`. | |

### Fila

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_QUEUE` | `inline` | `jobqueue/__init__.py` | ativo | `inline` executa em threads do próprio processo. `celery` usa Celery com Redis. | Qualquer valor diferente de `celery` é tratado como `inline`. |
| `CONVERTER_WORKERS` | `2` | `jobqueue/inline.py`; `jobqueue/scheduler.py` | ativo com `inline` | Número de execuções em paralelo no modo `inline`. | No Compose o motor define `1`, e o worker Celery usa `-c 2`. |
| `CONVERTER_TASK_TIMEOUT_SECONDS` | `0` | `jobqueue/inline.py`; `engine/ocr/engines.py`; `worker/celery_app.py` | ativo | Prazo global por documento. `0` desativa o prazo. | O mesmo valor é passado como timeout ao Tesseract. |
| `REDIS_URL` | `redis://localhost:6379/0` | `jobqueue/celery_queue.py`; `worker/celery_app.py` | ativo só com `celery` | Broker e backend do Celery. | Sem efeito no modo `inline`. |

### Limites

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_MAX_PAGES` | nenhum | `config.py` (`max_pages()`) | obsoleto | Teto de páginas legado. | Sem efeito enquanto `LARGE_DOCUMENT_PIPELINE_ENABLED=true`, que é o padrão. Mesmo com a flag em `false`, o motor não recusa nem trunca: o inspetor só registra um aviso (`engine/inspector/pdf_inspector.py`). |
| `CONVERTER_MAX_PDF_BYTES` | `52428800` (50 MiB) | `config.py` (`max_bytes()`); `api/service.py`; `engine/inspector/pdf_inspector.py`; `api/public_routes.py` | ativo | Tamanho máximo de um PDF. | O limite efetivo é o menor entre este valor e `UPLOAD_MAX_BYTES`. |
| `CONVERTER_MAX_IMAGE_FRAMES` | `500` | `ingestion/image_input.py`; `api/capabilities.py` | ativo | Guarda de recurso: quadros máximos de uma imagem multiquadro (TIFF, GIF). | Não é teto de páginas de PDF. |
| `CONVERTER_MAX_IMAGE_MEGAPIXELS` | `40.0` | `ingestion/image_input.py`; `api/capabilities.py` | ativo | Megapixels máximos por imagem. | Guarda de recurso. |
| `CONVERTER_MAX_JOB_MEGAPIXELS` | `200.0` | `ingestion/image_input.py`; `api/capabilities.py` | ativo | Megapixels somados de um trabalho. | Guarda de recurso. |
| `UPLOAD_MAX_BYTES` | `52428800` (50 MiB) | `config.py` (`max_bytes()`); `api/batch_service.py` | ativo | Limite por arquivo: envio simples (`min` com `CONVERTER_MAX_PDF_BYTES`) e registro de item de lote (`FILE_TOO_LARGE`). | No NestJS, o mesmo nome limita o envio legado de arquivo único; o registro de item de lote usa `UPLOAD_MAX_FILE_BYTES`, sem teto por padrão. Veja [API pública](#api-pública-nestjs). |
| `UPLOAD_MAX_PAGES` | nenhum | `config.py` (`max_pages()`) | obsoleto | Teto de páginas legado no motor. | Mesma regra de `CONVERTER_MAX_PAGES`. No NestJS, a regra é outra: ver [API pública](#api-pública-nestjs). |
| `CONVERTER_TARGET_FORMATS` | `xlsx,txt,csv` | `config.py` (`target_formats()`); `worker/tasks.py`; `api/service.py` | ativo | Formatos gerados quando a conversão não pede nenhum. | Valores fora de `xlsx`, `txt` e `csv` são ignorados. Se nada sobrar, usa `xlsx`. Imagens geram só `txt`. |

### PDFs grandes e lotes

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `LARGE_DOCUMENT_PIPELINE_ENABLED` | `true` | `config.py` (`max_pages()`); `api/public_routes.py` | ativo | Chave do processamento integral do documento. Com `true`, nenhum teto de páginas é aplicado. | No NestJS é só informativa. |
| `BATCH_UPLOAD_ENABLED` | `true` | `api/batch_service.py`; `api/capabilities.py`; `api/public_routes.py` | ativo | Liga lotes e envio em partes no motor. | No NestJS, `false` também recusa novos lotes (`batches.service.ts`). |
| `MAX_FILES_PER_BATCH` | nenhum | `api/capabilities.py`; `api/public_routes.py` | anunciado mas não aplicado | Quantidade máxima de arquivos por lote. | Nada recusa um lote pelo número de arquivos. |
| `UPLOAD_PART_BYTES` | `8388608` (8 MiB) | `config.py`; `api/upload_service.py`; `api/batch_service.py`; `api/capabilities.py` | ativo | Tamanho de cada parte no envio em partes. | É parâmetro de transporte, não limite de documento. Use o mesmo valor no motor e no NestJS. |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | `api/upload_service.py`; `api/batch_service.py` | ativo | Validade de uma sessão de envio que não foi concluída. | |
| `UPLOAD_CONCURRENCY` | `3` | `config.py` (campo `upload_concurrency`) | lido mas sem efeito | Paralelismo de envio. | Nenhum leitor no motor. A web usa 3 fixo (`MAX_PARALLEL_TRANSFERS`). |
| `CHECKPOINT_REUSE_ENABLED` | `true` | `engine/pipeline.py` | ativo | Reaproveita os checkpoints por página quando a mesma execução é retomada. | |
| `CONVERTER_PAGE_WINDOW` | `25` | `config.py` | lido mas sem efeito | Janela de páginas por checkpoint. | Sem leitor. |
| `CONVERTER_OCR_CALL_TIMEOUT_SECONDS` | `300` | `config.py` | lido mas sem efeito | Timeout de uma chamada de OCR. | Sem leitor. O timeout efetivo é `GLMOCR_REQUEST_TIMEOUT` no GLM-OCR e `CONVERTER_TASK_TIMEOUT_SECONDS` no Tesseract. |

### OCR e GLM-OCR

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_RENDER_DPI` | `300` | `engine/ocr/ocr_runner.py`; `engine/ocr/rasterize.py`; `worker/tasks.py` | ativo | DPI usado para rasterizar páginas antes do OCR. | No caminho de OCR observado, o `ocr.dpi` de cada conversão não é passado: vale este valor. Veja [Opções por conversão](#opções-por-conversão). |
| `CONVERTER_OCR_ENABLED` | `true` | `config.py`; `api/service.py`; `worker/tasks.py`; `api/internal_routes.py` | ativo | Valor padrão de `ocr.enabled`. | |
| `CONVERTER_OCR_MIN_CONFIDENCE` | `0.45` | `engine/ocr/ocr_runner.py` | ativo | Confiança mínima de uma palavra reconhecida. | |
| `CONVERTER_OCR_MAX_PIXELS` | `40000000` | `engine/ocr/rasterize.py` | ativo | Teto de pixels de uma página rasterizada. Acima disso, a escala é reduzida. | |
| `CONVERTER_MAX_FALLBACK_ATTEMPTS` | `2` | `engine/ocr/ocr_runner.py` | ativo | Tentativas extras com imagem melhorada quando o OCR falha. | Total de tentativas = valor + 1. |
| `CONVERTER_OCR_ENGINE` | vazio (automático) | `engine/ocr/engines.py` (`os.getenv`) | ativo, só variável de processo | Motor preferido: `glm-ocr`, `rapidocr` ou `tesseract`. | Se o preferido não estiver disponível, a ordem padrão é usada. Sem nenhum motor, a região fica sem texto e registra `OCR_UNAVAILABLE`. |
| `TESSERACT_CMD` | vazio (usa o PATH) | `engine/ocr/engines.py` (`os.environ`) | ativo, só variável de processo | Caminho do executável do Tesseract. | No Windows, se não estiver no PATH, o motor procura em `C:\Program Files\Tesseract-OCR\` e `C:\Program Files (x86)\Tesseract-OCR\` e define a variável sozinho. |
| `GLMOCR_ENABLED` | `true` | `engine/ocr/glmocr.py` | ativo | Habilita o GLM-OCR. | Mesmo com `true`, só é usado se o Ollama responder e o modelo estiver instalado. A verificação fica em cache por `GLMOCR_PROBE_CACHE_SECONDS`. |
| `GLMOCR_HOST` | `127.0.0.1` | `engine/ocr/glmocr.py` | ativo | Host do Ollama. | |
| `GLMOCR_PORT` | `11434` | `engine/ocr/glmocr.py` | ativo | Porta do Ollama. | |
| `GLMOCR_MODEL` | `glm-ocr:latest` | `engine/ocr/glmocr.py` | ativo | Modelo usado no Ollama. | |
| `GLMOCR_REQUEST_TIMEOUT` | `300` | `engine/ocr/glmocr.py` | ativo | Timeout de cada chamada ao Ollama, em segundos. | É o timeout de OCR que de fato vale. |
| `GLMOCR_NUM_CTX` | `8192` | `engine/ocr/glmocr.py` | ativo | Janela de contexto enviada ao modelo. | |
| `GLMOCR_NUM_PREDICT` | `4096` | `engine/ocr/glmocr.py` | ativo | Máximo de tokens gerados por página. | |
| `GLMOCR_KEEP_ALIVE` | `30m` | `engine/ocr/glmocr.py` | ativo | Tempo que o modelo permanece carregado no Ollama. | |
| `GLMOCR_NUM_GPU` | vazio (automático) | `engine/ocr/glmocr.py` | ativo | Número de camadas na GPU. Vazio deixa o Ollama decidir. | Verifique com `ollama ps`. |
| `GLMOCR_CONFIDENCE` | `0.90` | `engine/ocr/glmocr.py` | ativo | Confiança constante atribuída às palavras do modelo de visão. | O modelo não fornece confiança por palavra. |
| `GLMOCR_PROBE_CACHE_SECONDS` | `60` | `engine/ocr/glmocr.py` | ativo | Tempo em cache da verificação de disponibilidade do Ollama. | |

### Assistente

O assistente é opcional. Desligado por padrão, ele não afeta as conversões.

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `ASSISTANT_ENABLED` | `false` | `assistant/service.py`; `api/capabilities.py` | ativo | Liga o assistente. | Precisa do Ollama com os modelos abaixo. |
| `ASSISTANT_PROVIDER` | `ollama` | `assistant/service.py` | só informativo | Nome do provedor. | Só o Ollama existe. Não há ramificação por este valor. |
| `ASSISTANT_HOST` | `127.0.0.1` | `assistant/ollama_client.py`; `assistant/service.py` | ativo | Host do Ollama para o assistente. | Também define se a instância é reportada como local. |
| `ASSISTANT_PORT` | `11434` | `assistant/ollama_client.py` | ativo | Porta do Ollama para o assistente. | |
| `ASSISTANT_MODEL` | `qwen3.5:4b` | `assistant/ollama_client.py`; `assistant/service.py` | ativo | Modelo de chat. | |
| `ASSISTANT_CONTEXT_TOKENS` | `8192` | `assistant/ollama_client.py` | ativo | Janela de contexto do chat (`num_ctx`). | |
| `ASSISTANT_MAX_OUTPUT_TOKENS` | `1024` | `assistant/ollama_client.py` | ativo | Máximo de tokens na resposta (`num_predict`). | |
| `ASSISTANT_TIMEOUT_SECONDS` | `120` | `assistant/ollama_client.py` | ativo | Timeout de uma resposta. | |
| `ASSISTANT_MAX_CONCURRENT` | `1` | `api/knowledge_routes.py` | ativo | Respostas simultâneas do assistente. | |
| `ASSISTANT_THINKING` | `false` | `assistant/ollama_client.py` | ativo | Ativa o fluxo de raciocínio do Qwen3.5. | Deixar desligado evita que a resposta fique sem tokens. |
| `EMBEDDING_MODEL` | `nomic-embed-text` | `assistant/ollama_client.py`; `assistant/service.py` | ativo | Modelo de embeddings para busca. | Instale com `ollama pull nomic-embed-text`. |
| `ASSISTANT_REQUIRE_SOURCES` | `true` | `assistant/service.py` | ativo | Exige citações de fonte nas respostas. | Sem citações, o assistente devolve um aviso de evidência insuficiente, em vez de uma resposta. |

### Segurança

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_INTERNAL_TOKEN` | `dev-internal-token-change-me` | `api/deps.py` (`internal_guard`); `worker/tasks.py` | ativo | Token entre a API NestJS e o motor, enviado no cabeçalho `x-internal-token`. | Deve ser idêntico nos dois serviços. Se ficar no padrão, o motor avisa no início. Veja [Segredos locais](#segredos-locais). |
| `JWT_SECRET` | `dev-jwt-secret-change-me` | `api/deps.py` | ativo | Chave que assina e valida os tokens locais (HS256). | Se ficar no padrão, o motor avisa no início. |
| `JWT_EXPIRES_IN_SECONDS` | `604800` (7 dias) | `api/deps.py`; `api/public_routes.py` | ativo | Validade do token local, em segundos. | Não confundir com `JWT_EXPIRES_IN=7d` do NestJS. |
| `AUTH_MODE` | `optional` | `api/deps.py`; `api/public_routes.py` | ativo | `optional` ou `required`. | Não há validação do valor. Só `required` é checado explicitamente; qualquer outro valor se comporta como `optional`. |

### Retenção

A retenção está **anunciada, mas não aplicada**. Nenhum job apaga arquivos por prazo. Os valores aparecem em `/settings` e são gravados como metadado nos artefatos.

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `RETENTION_PDF_DAYS` | `7` | `api/public_routes.py` | anunciado mas não aplicado | Prazo anunciado para o PDF original. | Nenhum código apaga o PDF pelo prazo. |
| `RETENTION_XLSX_DAYS` | `30` | `repositories/conversion_repository.py` (gravado como `retention_days` no artefato); `api/public_routes.py` | anunciado mas não aplicado | Prazo anunciado para o XLSX e o texto. | Registrado no metadado, mas nenhum expurgo lê esse campo. |
| `RETENTION_AUDIT_DAYS` | `90` | `repositories/conversion_repository.py`; `api/public_routes.py` | anunciado mas não aplicado | Prazo anunciado para a trilha de auditoria. | Mesmo caso de `RETENTION_XLSX_DAYS`. |
| `ARTIFACT_COMPRESSION` | `true` | nenhum | lido mas sem efeito | Compressão de artefatos. | Sem leitor. |

### Integração

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `CONVERTER_PUBLIC_URL` | `http://localhost:8000` | nenhum | lido mas sem efeito | URL pública do motor. | Sem leitor. |
| `PUBLIC_API_URL` | `http://localhost:3000` | nenhum | lido mas sem efeito | URL pública da API. | Sem leitor no motor. No NestJS também não há consumidor. |
| `CONVERTER_CALLBACK_URL` | vazio | `worker/tasks.py` (`_notify_node`) | ativo quando definida | Endereço que recebe o andamento da conversão. No Compose: `http://api:3000/api/v1/internal/hooks/converter`. | Sem ela, o motor não envia essas notificações. |

---

## API pública (NestJS)

Caminho de origem: `apps/api/src/config/configuration.ts`. Os padrões abaixo são os do código. O total é de 41 variáveis.

### Runtime e API

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `RUNTIME_MODE` | `full` | só informativo | Modo declarado. | Padrão **diferente** do motor, que é `standalone`. |
| `ENGINE_VERSION` | `1.0.0` | só informativo | Versão reportada em `/settings`. | |
| `LOG_LEVEL` | `info` | só informativo | Nível de log declarado. | Nenhum logger do NestJS lê este valor. Aceita `debug`, `info`, `warn`, `error`. |
| `API_PORT` | `3000` | ativo | Porta da API. | |
| `API_HOST` | `127.0.0.1` | ativo | Interface onde a API escuta. | No Docker a imagem define `0.0.0.0`. Mantenha `127.0.0.1` no uso local. |
| `API_DATABASE_URL` | `file:./data/api.db` | ativo | Banco da API, lido pelo Prisma no `schema.prisma`. | Caminho SQLite resolvido pelo Prisma em relação ao schema (`apps/api/prisma/`). |
| `JWT_SECRET` | `dev-only-insecure-secret-change-me` | ativo | Chave que assina os tokens da API. | Padrão diferente do motor. Veja [Segredos locais](#segredos-locais). |
| `JWT_EXPIRES_IN` | `7d` | ativo | Validade do token da API (aceita sufixos de tempo). | Nome e unidade diferentes de `JWT_EXPIRES_IN_SECONDS` do motor. |
| `AUTH_MODE` | `optional` | ativo | `optional` ou `required`. Valores inválidos caem para `optional`. | |
| `UPLOAD_MAX_BYTES` | `52428800` | ativo | Limite do corpo de requisição e do endpoint legado de arquivo único. | |
| `UPLOAD_MAX_PAGES` | nenhum | obsoleto | Teto de páginas do endpoint legado. | Se definida, `FileValidationService` recusa PDFs acima dela com `TOO_MANY_PAGES`. Sem ela, não há teto. |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | ativo | Origens liberadas no CORS. | |
| `PUBLIC_API_URL` | `http://localhost:3000` | lido mas sem efeito | URL pública da API. | Sem consumidor. |

### Upload e lotes

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `UPLOAD_PART_BYTES` | `8388608` | ativo | Tamanho da parte no envio em partes (mínimo 64 KiB). | Usado em `batches.service.ts`. |
| `UPLOAD_PART_MAX_BYTES` | `33554432` | anunciado mas não aplicado | Maior parte aceita, anunciada em `/settings`. | Nenhum código recusa parte acima dele. Nunca fica abaixo de `UPLOAD_PART_BYTES`. |
| `UPLOAD_CONCURRENCY` | `3` | lido mas sem efeito | Paralelismo de envio. | Lida, mas nem anunciada. A web usa 3 fixo. |
| `MAX_FILES_PER_BATCH` | nenhum | anunciado mas não aplicado | Máximo de arquivos por lote. | Anunciado em `/settings`. Nada recusa um lote pelo número de arquivos. |
| `UPLOAD_MAX_FILE_BYTES` | nenhum (sem teto) | ativo quando definida | Tamanho máximo de um arquivo do lote. | Aplicada em `batches.service.ts`, com `FILE_TOO_LARGE`. Não está no `.env.example`. |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | ativo | Validade de uma sessão incompleta. | |
| `UPLOAD_MAX_DOCUMENT_BYTES` | nenhum | lido mas sem efeito | Teto de bytes por documento. | Sem consumidor. |

### Funcionalidades

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `BATCH_UPLOAD_ENABLED` | `true` | ativo | Liga lotes. | `false` recusa novos lotes. |
| `LARGE_DOCUMENT_PIPELINE_ENABLED` | `true` | só informativo | Reportada como `full_document` em `/settings`. | O efeito real está no motor. |
| `RESULT_COMPACT_ENABLED` | `true` | só informativo | Reportada em `/settings`. | Nenhum outro código a lê. |
| `MCP_ENABLED` | `true` | só informativo | Reportada em `/settings`. | Não desliga o adaptador MCP. |

### Motor (cliente)

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `CONVERTER_INTERNAL_URL` | `http://localhost:8000` | ativo | Endereço do motor visto pela API. | Usado em `converter.client.ts` e `engine-proxy.service.ts`. |
| `CONVERTER_INTERNAL_TOKEN` | `dev-internal-token-change-me` | ativo | Token enviado ao motor em `x-internal-token`. | Deve ser igual ao do motor. |
| `CONVERTER_TIMEOUT_MS` | `30000` | ativo | Timeout de cada chamada ao motor. | Mínimo de 1000 ms. |
| `CONVERTER_POLL_INTERVAL_MS` | `800` | ativo | Intervalo de consulta ao andamento. | Mínimo de 100 ms. |
| `CONVERTER_HEALTH_CACHE_MS` | `30000` | ativo | Cache da verificação de saúde do motor. | Mínimo de 1000 ms. |

### Armazenamento

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `STORAGE_BACKEND` | `local` | ativo | `local` ou `s3`. | Valor inválido cai para `local`. |
| `STORAGE_LOCAL_ROOT` | `./data/storage` | ativo | Pasta local dos arquivos. | Resolvida em relação ao **diretório de execução** (`process.cwd()`), diferente do motor. |
| `S3_ENDPOINT` | nenhum | ativo com `s3` | URL do S3 ou MinIO. | |
| `S3_REGION` | `us-east-1` | ativo com `s3` | Região. | |
| `S3_ACCESS_KEY` | nenhum | ativo com `s3` | Chave de acesso. | |
| `S3_SECRET_KEY` | nenhum | ativo com `s3` | Segredo de acesso. | |
| `S3_BUCKET` | `pdf2x` | ativo com `s3` | Bucket. | |
| `S3_FORCE_PATH_STYLE` | `true` | ativo com `s3` | Estilo de caminho para o endpoint. | Não está no `.env.example`. |
| `S3_SECURE` | `false` | ativo com `s3` | Usa TLS. | |

### Retenção

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `RETENTION_PDF_DAYS` | `7` | anunciado mas não aplicado | Anunciado em `/settings`. | Nenhum expurgo aplica o prazo. |
| `RETENTION_XLSX_DAYS` | `30` | anunciado mas não aplicado | Anunciado em `/settings`. | Mesmo caso. |
| `RETENTION_AUDIT_DAYS` | `90` | anunciado mas não aplicado | Anunciado em `/settings`. | Mesmo caso. |

---

## Interface web (Vite)

Caminhos: `apps/web/vite.config.ts` e `apps/web/src/lib/api.ts`. Lidas de `apps/web/.env*`, não da raiz.

| Variável | Padrão | Lido em | Status | O que faz | Observações |
|---|---|---|---|---|---|
| `VITE_API_BASE` | `/api/v1` | `apps/web/src/lib/api.ts` (build) | ativo | Base da API usada pelo SPA. | Embutida no bundle. Mudar exige novo build. |
| `VITE_API_PROXY_TARGET` | `http://localhost:3000` | `apps/web/vite.config.ts` (dev) | ativo no dev | Destino do proxy `/api` do servidor de desenvolvimento. | Use `http://localhost:8000` no modo standalone. Não tem efeito no build de produção. |

---

## Adaptador MCP

Caminho: `apps/mcp/src/config.ts`. Lê apenas `process.env`, como descrito em [Como a configuração é carregada](#como-a-configuração-é-carregada).

| Variável | Padrão | Status | O que faz | Observações |
|---|---|---|---|---|
| `PDF2X_API_BASE_URL` | `http://localhost:8000` | ativo | Raiz da API. Aceita com ou sem `/api/v1`. | O padrão aponta para o **motor** (standalone). No modo full, defina `http://localhost:3000`. |
| `PDF2X_DEVICE_ID` | nenhum (gerado e persistido) | ativo | Identidade do dispositivo. | Com o valor do `pdf2x.device_id` do navegador, as conversões do MCP aparecem no mesmo histórico da web. 8 a 128 caracteres: letras, números, `.`, `_`, `:`, `-`. |
| `PDF2X_CONFIG_DIR` | `~/.pdf2x` | ativo | Pasta do `identity.json` e do `submissions.jsonl`. | Deve ser caminho absoluto. Não versionar. |
| `PDF2X_ALLOWED_ROOTS` | diretório pessoal do usuário | ativo | Pastas (separadas por vírgula) que o MCP pode ler e gravar. | Cada entrada deve ser absoluta. Vazio usa o diretório pessoal. |
| `PDF2X_TRANSFER_CONCURRENCY` | `3` | ativo | Partes enviadas em paralelo, somadas entre documentos. | Entre 1 e 32. |
| `PDF2X_MCP_TRANSPORT` | `stdio` | ativo | `stdio` ou `http`. | Outro valor impede a inicialização. |
| `PDF2X_MCP_PORT` | `7801` | ativo com `http` | Porta do transporte HTTP (`/mcp`). | Entre 1 e 65535. |
| `PDF2X_MCP_HOST` | `127.0.0.1` | ativo com `http` | Interface do transporte HTTP. | Mantenha `127.0.0.1`. |

---

## Docker Compose

Caminho: `infra/docker-compose.yml`. O Compose **não** lê o `.env` da raiz por conta própria. Ele lê `infra/.env`, variáveis do shell, ou o arquivo passado com `--env-file`. Os serviços não usam `env_file`. Para usar o `.env` da raiz:

```
cd infra
docker compose --env-file ../.env up --build
```

Variáveis de interpolação usadas pelo Compose:

| Variável | Padrão no Compose | Obrigatória | O que faz | Observações |
|---|---|---|---|---|
| `CONVERTER_INTERNAL_TOKEN` | nenhum | sim | Token entre API e motor. | Falha no `up` se não estiver definida. |
| `JWT_SECRET` | nenhum | sim | Chave dos tokens da API. | Falha no `up` se não estiver definida. |
| `AUTH_MODE` | `optional` | não | Modo de autenticação da API. | |
| `ALLOWED_ORIGINS` | `http://localhost:8080` | não | Origens liberadas na API. | |
| `UPLOAD_MAX_BYTES` | `52428800` | não | Limite de arquivo único. | |
| `WEB_PORT` | `8080` | não | Porta publicada da web, em `127.0.0.1`. | |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `pdf2x` | não | Credenciais e banco do PostgreSQL. | Troque em qualquer uso fora da sua máquina. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` / `S3_REGION` | `minioadmin` / `minioadmin` / `pdf2x` / `us-east-1` | não | Credenciais e bucket do MinIO. | Troque as credenciais do MinIO. |
| `RETENTION_PDF_DAYS` / `RETENTION_XLSX_DAYS` / `RETENTION_AUDIT_DAYS` | `7` / `30` / `90` | não | Prazos anunciados. | Não aplicados. Veja [Retenção](#retenção). |
| `LOG_LEVEL` | `INFO` | não | Nível de log do motor. | |
| `CONVERTER_RENDER_DPI` | `300` | não | DPI de rasterização. | |
| `CONVERTER_OCR_ENABLED` | `true` | não | OCR ligado por padrão. | |
| `CONVERTER_OCR_MIN_CONFIDENCE` | `0.45` | não | Confiança mínima de palavra. | |

Valores **fixos** no Compose, que não podem ser trocados pelo `.env`: `RUNTIME_MODE=full` e `STORAGE_BACKEND=s3` no motor e na API, `CONVERTER_QUEUE=celery`, `CONVERTER_WORKERS=1` no motor, `API_HOST=0.0.0.0` e `API_PORT=3000` na API, `CONVERTER_CALLBACK_URL` apontando para `http://api:3000/api/v1/internal/hooks/converter`, `CONVERTER_TIMEOUT_MS=30000`, `S3_FORCE_PATH_STYLE=true`, `VITE_API_BASE=/api/v1` no build da web. O Compose também **não** define `CONVERTER_MAX_PAGES` nem `UPLOAD_MAX_PAGES`. O motor e o worker não publicam portas; só a web é exposta, em `127.0.0.1`.

---

## Segredos locais

Há quatro segredos. Dois deles têm padrões de desenvolvimento diferentes entre o motor e a API.

| Segredo | Padrão no motor | Padrão no NestJS | Precisa coincidir? |
|---|---|---|---|
| `CONVERTER_INTERNAL_TOKEN` | `dev-internal-token-change-me` | `dev-internal-token-change-me` | **Sim**. A API envia o token e o motor o confere. |
| `JWT_SECRET` | `dev-jwt-secret-change-me` | `dev-only-insecure-secret-change-me` | Os padrões **são diferentes**. No modo full, defina o mesmo valor nos dois. |

Os padrões existem para o modo standalone funcionar sem configuração. O motor avisa no início (`segredos com valor padrão de desenvolvimento em uso`) quando um deles fica no padrão. O aviso não impede a inicialização. Antes de expor o serviço em qualquer rede, troque os dois.

No modo full com um único `.env` na raiz, os dois serviços leem o mesmo valor. Se você separar os arquivos, repita o mesmo valor em ambos.

**Observação sobre o JWT.** No código atual, não encontrei repasse do cabeçalho `Authorization` da API para o motor (`converter.client.ts`, `engine-proxy.service.ts`). Por isso, o `JWT_SECRET` compartilhado é uma boa prática de consistência, e não um requisito verificado do fluxo full.

**Como gerar um valor.** Com Python:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

No PowerShell (funciona no Windows PowerShell 5.1):

```
$b = New-Object byte[] 48; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
```

---

## Opções por conversão

Estas opções vêm no corpo da requisição de conversão e seguem `ConversionOptionsV2` em `packages/contracts/src/options.ts`. Os nomes são em snake_case. Opções não enviadas usam o padrão.

| Campo | Valores | Padrão | Status | Observações |
|---|---|---|---|---|
| `schema_version` | `2` | `2` | ativo | Única versão aceita. |
| `target_formats` | lista de `xlsx`, `txt`, `csv` | PDF: `xlsx`, `txt`, `csv`. Imagem: `txt`. | ativo | Lista vazia é recusada. Imagens só geram `txt`. Pedir `xlsx` ou `csv` para imagem é recusado. |
| `ocr.enabled` | `true` ou `false` | `true` (ou `CONVERTER_OCR_ENABLED`) | ativo | Desliga o OCR da conversão. |
| `ocr.languages` | lista de códigos, até 8 | `["por", "eng"]` | aceito, sem efeito | Não é usada. O Tesseract recebe `por+eng` fixo; nenhum chamador passa `lang`. |
| `ocr.dpi` | inteiro de 72 a 600, ou nulo | nulo (usa `CONVERTER_RENDER_DPI`) | aceito, sem efeito na rasterização | Entra na chave de checkpoint, mas no caminho de OCR observado a página é rasterizada sem este valor. |
| `ocr.strategy` | `auto`, `native`, `ocr`, `hybrid` | `auto` | aceito, sem efeito | Não é lida pelo motor. |
| `text.newline` | `lf`, `crlf` | `lf` | ativo | Fim de linha do TXT. |
| `text.page_separator` | `form_feed`, `marker`, `none` | `form_feed` | ativo | Separador entre páginas no TXT. |
| `text.include_table_text` | `true` ou `false` | `true` | ativo | Inclui o texto das tabelas no TXT. |
| `csv.profile` | `interoperable`, `excel_br` | `interoperable` | ativo | Perfil de formatação do CSV. |
| `csv.delimiter` | um caractere ou nulo | nulo (automático) | ativo | |
| `csv.formula_policy` | `safe`, `raw` | `safe` | ativo | `safe` é o padrão e protege contra fórmulas nas células; `raw` desliga essa proteção. |
| `csv.table_ids` | lista de IDs, ou nulo | nulo (todas) | ativo | Restringe quais tabelas saem no CSV. |
| `csv.value_mode` | `original`, `canonical` | `original` | ativo | |

**Teto de páginas.** As chaves `max_pages`, `maxPages`, `max_pages_per_document` e `page_limit` são recusadas com `INVALID_OPTION` quando recebem um valor (nulo é aceito e ignorado). Não há limite de páginas por requisição.

**Chaves desconhecidas.** Dentro de `ocr`, `text` e `csv`, chaves não previstas são recusadas (validação estrita). No nível de topo, há aceitação de campos planos antigos (`ocr_enabled`, `dpi`, `text_newline`, `csv_profile` e similares), por compatibilidade com clientes anteriores.

---

## Receitas

### Standalone local, OCR com Tesseract

1. Instale o Tesseract com o pacote de português. No Windows, o instalador padrão é encontrado sozinho em `C:\Program Files\Tesseract-OCR\`. Se estiver em outro lugar, defina `TESSERACT_CMD` **como variável de processo**:

   ```
   $env:TESSERACT_CMD="<caminho-do-tesseract>\tesseract.exe"
   ```

2. Crie o ambiente e instale o motor:

   ```
   cd services\converter
   python -m venv .venv
   .venv\Scripts\pip install -e ".[test]"
   ```

3. Gere a interface, para o motor servi-la:

   ```
   npm --prefix apps/web run build
   ```

4. Suba o motor em `http://127.0.0.1:8000`, ou use `starter-server.cmd` na raiz:

   ```
   cd services\converter
   .venv\Scripts\python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
   ```

   A porta `CONVERTER_PORT` não muda nada: o uvicorn recebe `--port` na linha de comando.

### GLM-OCR com GPU, via Ollama

1. Instale o Ollama e o modelo:

   ```
   ollama pull glm-ocr:latest
   ollama serve
   ```

2. Confira a GPU: `ollama ps` mostra os modelos carregados e o processador usado.

3. Nada mais é necessário: `GLMOCR_*` já têm padrões válidos. O motor escolhe o GLM-OCR quando o Ollama responde e o modelo existe. Para forçar o Tesseract como preferido, defina `CONVERTER_OCR_ENGINE=tesseract` como variável de processo. Se ele não estiver disponível, a ordem padrão é usada.

### Assistente local

1. Instale os modelos:

   ```
   ollama pull qwen3.5:4b
   ollama pull nomic-embed-text
   ```

2. No `.env`, defina `ASSISTANT_ENABLED=true` e reinicie o motor. Mantenha `ASSISTANT_THINKING=false`.

### Modo full local

1. Crie `<raiz>/.env` com o bloco de [modo full](#exemplos-de-env). Gere `JWT_SECRET` e `CONVERTER_INTERNAL_TOKEN` com os comandos de [Segredos locais](#segredos-locais).

2. Crie as tabelas da API:

   ```
   npm --prefix apps/api run db:push
   ```

3. Em três terminais:

   ```
   cd services\converter
   $env:RUNTIME_MODE="full"; .venv\Scripts\python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
   ```

   ```
   npm --prefix apps/api run start:dev
   ```

   ```
   npm --prefix apps/web run dev
   ```

4. A interface fica em `http://localhost:5173`, com o proxy `/api` apontando para a API na porta 3000. Se o proxy precisar de outro destino, defina `VITE_API_PROXY_TARGET` em `apps/web/.env`.

### Docker Compose

```
cd infra
docker compose --env-file ../.env up --build
```

A interface fica em `http://localhost:8080`. Para mudar a porta, defina `WEB_PORT` no `.env`. O Compose publica só `127.0.0.1:${WEB_PORT}`.

### Mudar portas

| O que mudar | Onde |
|---|---|
| Porta do motor | Não há variável que funcione. Mude o comando do uvicorn, e também `CONVERTER_INTERNAL_URL` da API e `VITE_API_PROXY_TARGET` da web, se necessário. |
| Porta da API | `API_PORT`, e `VITE_API_PROXY_TARGET` da web para acompanhar. |
| Porta da web no Compose | `WEB_PORT`. |
| Porta do MCP HTTP | `PDF2X_MCP_PORT`. |

### MCP com a mesma identidade do navegador

1. Abra a interface no navegador e, no console do DevTools, rode `localStorage.getItem('pdf2x.device_id')`.
2. Defina `PDF2X_DEVICE_ID` com esse valor no ambiente do host MCP, junto com `PDF2X_API_BASE_URL`. Em modo full, use `http://localhost:3000`:

   ```json
   "env": {
     "PDF2X_API_BASE_URL": "http://localhost:3000",
     "PDF2X_DEVICE_ID": "<valor de pdf2x.device_id>"
   }
   ```

3. As conversões feitas pelo MCP passam a aparecer no histórico da interface.

---

## Pontos de atenção

Itens que o código contradiz ou que podem surpreender quem lê a configuração.

1. **Teto de páginas no motor.** `CONVERTER_MAX_PAGES` e `UPLOAD_MAX_PAGES` nunca recusam nem truncam documentos no motor. Com `LARGE_DOCUMENT_PIPELINE_ENABLED=false`, o inspetor só registra um aviso. No NestJS, `UPLOAD_MAX_PAGES` se aplica sempre que é definida e rejeita com `TOO_MANY_PAGES`.
2. **Opções de OCR aceitas e ignoradas.** `ocr.languages`, `ocr.strategy` e, no caminho observado, `ocr.dpi`. O Tesseract usa `por+eng` fixo. Veja [Opções por conversão](#opções-por-conversão).
3. **Retenção não é aplicada.** Os três prazos aparecem em `/settings`, e os de XLSX e auditoria são gravados como metadado, mas nada apaga arquivos. Trate-os como informação.
4. **Limites de envio anunciados.** `MAX_FILES_PER_BATCH` e `UPLOAD_PART_MAX_BYTES` aparecem em `/settings`, mas nada os impõe. `UPLOAD_CONCURRENCY` nem chega a ser anunciada: a web usa 3 fixo.
5. **Dois nomes para o mesmo conceito de validade do token.** `JWT_EXPIRES_IN=7d` no NestJS e `JWT_EXPIRES_IN_SECONDS=604800` no motor. Ajuste os dois se mudar um deles.
6. **Padrões de modo diferentes.** `RUNTIME_MODE` é `standalone` no motor e `full` no NestJS. Como é só informativo, o impacto é apenas no que aparece em `/settings`.
7. **MCP aponta para o motor por padrão.** `PDF2X_API_BASE_URL` padrão é `http://localhost:8000`. Em modo full, defina `http://localhost:3000`.
8. **Vite não lê o `.env` da raiz.** `VITE_API_BASE` e `VITE_API_PROXY_TARGET` precisam estar em `apps/web/.env`, ou no ambiente do shell.
9. **Caminhos relativos ao diretório de execução.** O NestJS resolve `.env` e `STORAGE_LOCAL_ROOT` em relação a onde o processo roda. O motor resolve os seus caminhos em relação a `services/converter/`.
10. **Variáveis que não são lidas do `.env`.** `LOG_JSON`, `CONVERTER_OCR_ENGINE` e `TESSERACT_CMD` precisam ser variáveis de processo.
11. **CORS vazio libera tudo.** `ALLOWED_ORIGINS=` vazio faz o motor aceitar qualquer origem.
12. **Compose não lê o `.env` da raiz sozinho.** Use `--env-file ../.env`, como em [Docker Compose](#docker-compose).
13. **Segredos de desenvolvimento.** Os quatro segredos têm padrões públicos no código. Troque-os antes de expor qualquer serviço.
