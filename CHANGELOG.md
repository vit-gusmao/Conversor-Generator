# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/), e o
projeto adota [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não lançado]

### Corrigido — dados, filas, lotes, limites e pacote Windows

- **Separador decimal:** uma cauda de 4 ou mais dígitos após vírgula ou ponto passa a
  ser parte decimal (antes podia ser lida como milhar). Vale para o valor de cada
  célula (`engine/normalization/normalizer.py`).
- **CSV canônico:** `csv.value_mode=canonical` converte apenas números que
  inequivocamente são pt-BR ou en; identificadores com zeros à esquerda e texto livre
  permanecem inalterados (`exports/csv_writer.py`).
- **Fila durável:** jobs do motor e de indexação de conhecimento usam lease com
  `fence`; uma escrita de um worker com fence antigo é rejeitada. A entrega da outbox
  da API usa lease de 60 s.
- **Estado de lote:** o status do lote é derivado de forma única a partir dos
  contadores, do selamento e do pedido de cancelamento (`_batch_status` em
  `api/batch_service.py`).
- **Teto de tamanho:** o envio simples (`POST /conversions`) respeita o menor valor
  entre `UPLOAD_MAX_BYTES` e `CONVERTER_MAX_PDF_BYTES`. Lotes e envio por partes não
  têm teto de documento; cada parte acima de `part_size` é recusada com `413
  FILE_TOO_LARGE`.
- **Aplicativo Windows:** o executável sai com o nome `conversor-generator.exe`
  (`executableName` do electron-builder) e os `extraResources` apontam para o backend
  e a SPA corretos; o workflow `desktop.yml` usa o cwd correto no CI.
- **Inicializador Windows:** `starter-local-server.cmd` procura Python 3.11+ (prefere 3.12), recria o venv quando
  necessário, reinstala as dependências só quando `pyproject.toml` muda, compila a
  interface quando falta e aguarda `/health`.

### Plano — conclusão dos itens T8, T10 a T14

- **T8:** calendário determinístico (DST e fuso IANA), claims com fence, executor
  retomável, indexação durável com recuperação de jobs órfãos e limpeza do histórico
  de automação. Retentativas de execução com espera de 30, 120 e 300 s. O `run-now`
  responde `202` com `queued` e a flag `executor_active`.
- **T10:** telas de Fontes (cadastro, validação e importação) e de Agendamentos
  (lista, formulário e ocorrência).
- **T11:** runtime empacotável com diretórios explícitos (`APP_DATA_DIR`, `WORK_DIR`,
  `LOG_DIR`), entry point congelável (`desktop_entry.py`), spec do PyInstaller em
  `packaging/converter.spec` e migração de banco com lock, backup (5 mais recentes)
  e recusa de schema mais novo.
- **T12:** shell Electron com supervisor do backend, isolamento de contexto,
  perfil atômico e IPC validado.
- **T13:** diagnóstico real de GLM-OCR e do LLM local (visão e geração), gerenciamento
  de modelos pelo aplicativo e importação de GGUF por Modelfile gerado.
- **T14:** CI Windows de empacotamento com smoke test do pacote, documentação
  operacional e flags publicadas em `/capabilities`.

### Adicionado — fontes, planilhas, agendador e desktop

- **Contratos v3 de opções** com seção `spreadsheet` (abas, cabeçalho, encoding e
  dialeto CSV, política de fórmulas), `reader_version` estampada pelo servidor e
  fingerprint estável; v2 continua idêntico. Fixtures compartilhados garantem
  paridade Python/TypeScript.
- **Entrada XLSX/CSV**: leitura por conteúdo (OOXML), orçamentos de expansão,
  preservação de valores originais e endereços reais de célula, fórmulas sem
  cache como texto com aviso, CSV com encoding/dialeto explícitos. Saídas XLSX
  normalizado, TXT e CSV.
- **Fontes autorizadas**: raízes concedidas, contenção por componentes, rejeição
  de UNC/rede, junctions não seguidas, snapshots com hash e detecção de arquivo
  em edição/bloqueado.
- **Importação durável**: idempotência por chave, deduplicação por conteúdo e
  opções, retomada após queda sem duplicar lote/conversão, estados parcial e
  sem-novidades.
- **Agendador** diário/intervalo com fuso IANA, DST determinístico, políticas de
  atraso e sobreposição, leases com fence, histórico e eventos SSE.
- **Executor retomável** com fases e snapshot congelado da fonte; **indexação
  agendada durável** com recuperação de jobs órfãos.
- **Telas** de Fontes, detalhe da fonte, lista/formulário de agendamento e
  ocorrência, com prévia das próximas cinco execuções.
- **Aplicativo Windows (Electron)**: supervisor do backend empacotável,
  diretórios explícitos em `userData`, perfil de modelos atômico, IPC validado,
  bandeja e configuração visual de GLM-OCR/LLM.
- **Diagnóstico real de modelos** (visão + geração) e importação de GGUF via
  Modelfile gerado pelo app.
- **Flags publicadas** em `/capabilities` e documentação em
  `docs/SOURCES_AND_AUTOMATION.md` e `docs/DESKTOP.md`.

### Adicionado

- **Lotes persistentes** (`/batches`): número ilimitado de documentos por lote,
  com itens idempotentes por `client_item_id` e selagem explícita.
- **Upload por partes** (`/uploads`): transporte retomável de 8 MiB, hash por
  parte, verificação do documento inteiro em `complete` e uma única conversão por
  item.
- **Fila durável** (`engine_jobs`): backlog no banco, lease com fence, retomada
  após queda e cancelamento durável arbitrado na publicação.
- **Checkpoints por página**: retomada de uma execução interrompida sem repetir
  OCR; checkpoints são escopados ao run, à versão do motor e às opções.
- **Resultado compacto**: `/conversions/:id/result-summary` e `/sheets`
  paginados; tela de resultado com um único seletor e uma única prévia.
- **Contratos canônicos** (`ConversionOptionsV2`) compartilhados por web, API e
  motor, com rejeição explícita de qualquer teto de páginas.
- **Adaptador MCP** em `apps/mcp` (envio multi-arquivo, acompanhamento e
  download por ferramentas).
- **`GET /api/v1/health/ready`** com `accepting_uploads`, `ocr_available` e
  `queue_pending`, sem hosts ou caminhos.
- Aba consolidada `Todos os dados` quando há duas ou mais abas estruturais: união
  de todas as colunas, com as colunas de origem `Aba` e `Página`. O download
  continua sendo um único `.xlsx`.
- Detecção de camada de texto invisível (modos de renderização `3 Tr`/`7 Tr`),
  com a issue `INVISIBLE_TEXT_LAYER`. Quando há imagem, o OCR visual prevalece.
- Reconstrução de tabelas sem geometria, pelo padrão do texto
  (`engine/tables/text_pattern.py`), quando nenhum detector geométrico produz candidato.
- Issue `OCR_LOW_LEGIBILITY` para glifos com menos de ~6 px de altura.
- Remoção de linhas idênticas repetidas em abas de registro, registrada em
  `DUPLICATE_ROWS_REMOVED`.
- Harness `services/converter/scripts/stress_gabarito.py`, que pontua a conversão
  contra `GABARITO.jsonl`. O corpus de stress é local e não é versionado.
- Testes `test_consolidated_sheet.py` e `test_stress_rules.py`.
- **Documentação completa da versão local**: `docs/FLOW.md` (fluxo completo),
  `docs/STORAGE.md` (onde ficam os dados), `docs/OPERATIONS.md` (backup, limpeza,
  logs e diagnóstico) e `docs/CONFIGURATION.md` (referência de configuração).

### Alterado

- O fluxo padrão **não tem teto de páginas**: `PAGES_NOT_PROCESSED` deixou de
  existir e `max_pages` virou configuração obsoleta nos dois modos.
- O prazo global de 600 s por documento foi removido por padrão
  (`CONVERTER_TASK_TIMEOUT_SECONDS=0`); o timeout passou a ser por unidade.
- O pipeline de PDF não carrega mais o arquivo inteiro em memória.
- O preview de planilha pagina em SQL em vez de carregar todas as células.
- A outbox do modo `full` usa claim com lease, não mais `pending → dispatched`
  sem exclusão mútua.
- Valores de AcroForm passam a ser dados do workbook, com o rótulo visual recuperado pela posição do widget.
- Separador decimal decidido por valor, e não pelo locale do documento.
- Números com mais de 15 dígitos significativos são mantidos como texto, para não serem arredondados pelo Excel.
- OCR: o raster original é a variante principal. A orientação é lida nas duas
  direções e as coordenadas são devolvidas ao espaço da página original.
- GLM-OCR: linhas e palavras recebem geometria a partir da tinta da própria linha.
- Stream: faixas de colunas por pitch e regra de 50% de coluna. Lattice: filtragem de texto rotacionado.
- Pares rótulo/valor são casados entre blocos; registros isolados são absorvidos pelo registro vizinho.
- Licença: projeto proprietário, com todos os direitos reservados (antes, a licença não estava definida).
- API NestJS e transporte HTTP do MCP escutam em `127.0.0.1` por padrão (`API_HOST`, `PDF2X_MCP_HOST`).
- Docker Compose publica a interface só em `127.0.0.1` e não define mais teto de 500 páginas.
- O projeto passa a ser exclusivamente local, sem hospedagem externa.

### Corrigido

- Modo `full` descartava `text`/`csv`/`target_formats` enviados pelo cliente.
- Uma parte de upload divergente podia apagar a parte já confirmada.
- Retransmissão idêntica de parte agora é no-op em vez de conflito.
- `seal` era bloqueado por itens `upload_failed` (estado definitivo).
- Migrações Alembic estavam atrasadas em relação aos modelos (tabelas de
  conhecimento, exportações e `options_json`).
- CORS da API passa a liberar o cabeçalho `Cache-Control`, enviado pelo stream
  SSE do frontend, para que o progresso funcione quando web e API estão em
  origens diferentes (front-end separado + backend).
- Correção de bug na sobreposição de regiões de layout.
- Tabelas: cabeçalho desgrudado do grid, células deslocadas, prosa rejeitada
  como tabela e cabeçalho acima do grid passam a ser tratados.
- Pré-processamento com OpenCV: a conversão de `ndarray` para PIL foi corrigida.

### Removido

- Arquivos de contribuição, canal de segurança e templates de issue/PR.
- Suporte a hospedagem na Vercel e no GitHub Pages: `vercel.json`,
  `apps/web/vercel.json`, `.vercelignore`, `.github/workflows/pages.yml`,
  `apps/web/scripts/prepare-pages.mjs`, o script `build:pages`, a etapa de CI
  correspondente e a variável `VITE_BASE_PATH` (a SPA é sempre servida a partir de `/`).
- `infra/nginx/nginx.conf` (proxy de borda não utilizado) e
  `infra/observability/otel-collector.yaml` (não referenciado).
- Variáveis mortas do `.env.example`: `OTEL_*`, `POSTGRES_HOST`/`POSTGRES_PORT` e
  `UPLOAD_PART_SIZE_BYTES`.

## [1.0.0] - 2026-11-16

### Adicionado

- Primeira versão pública da plataforma Conversor Generator, uma conversão
  documental auditável.
- **Motor de conversão** (`services/converter`, FastAPI): inspeção de PDF
  (criptografia, conteúdo ativo, limites), extração de texto nativo, OCR por
  página, layout e ordem de leitura, extração de campos `Rótulo: valor`, tabelas
  (lattice, stream, híbrida, Docling opcional), reconciliação multipágina,
  normalização de tipos, geração de XLSX e auditor do arquivo gerado.
- **Entradas e saídas**: PDF nativo, digitalizado e híbrido; imagens (PNG, JPEG,
  BMP, WebP, TIFF, GIF e outros codecs suportados). Saídas XLSX, TXT e CSV, geradas
  a partir do mesmo modelo intermediário.
- **Auditoria**: eventos encadeados por hash, manifesto com SHA-256, cobertura por
  página, proveniência célula a célula, quarentena de artefatos reprovados.
- **OCR**: GLM-OCR via Ollama (opcional, local), Tesseract e RapidOCR, com
  degradação explícita quando nenhum motor está disponível.
- **Assistente local (opcional)**: RAG sobre conversões do próprio usuário, com
  citações validadas pelo backend, usando Qwen3.5-4B via Ollama. Desativado por
  padrão.
- **API pública** (`/api/v1`) com dois modos de execução: `standalone` (FastAPI,
  SQLite, disco local, fila em threads) e `full` (NestJS 11 + Prisma com
  PostgreSQL ou SQLite, proxy para o motor, filas Celery/Redis opcionais, S3/MinIO
  opcional).
- **Interface** (`apps/web`, React 19 + Vite 7): upload com validação, progresso
  em tempo real por SSE com retomada, visualização do resultado e auditoria em 12
  abas.
- **Sessão local por navegador**: identidade por dispositivo, sem cadastro e sem
  senha, com isolamento de proprietário em todos os endpoints.
- **Pacote de contratos** (`packages/contracts`) com tipos zod e especificação
  OpenAPI.
- **Infraestrutura**: `infra/docker-compose.yml` (PostgreSQL, Redis, MinIO, motor,
  worker Celery, API e nginx), imagens Docker da API, do motor e da interface.
- **Documentação** em `docs/`: arquitetura, conversão, auditoria, banco,
  API e estratégia de testes.
- **Publicação**: `vercel.json` para a interface como SPA, workflow de integração
  contínua (`.github/workflows/ci.yml`) e guia de publicação em `docs/DEPLOY.md`.

### Conhecido e não implementado

- Estado `REVIEW_REQUIRED` e endpoint de revisão humana: ausentes. Veja
  [docs/FLOW.md](docs/FLOW.md#limitações-e-divergências-conhecidas).
- OCR por célula ou linha existe no componente, mas ainda não é acionado pelo
  pipeline.
- A execução com Docker Compose não foi verificada no ambiente de construção.
