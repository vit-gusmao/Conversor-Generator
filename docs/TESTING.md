# Estratégia de testes

## Pré-requisitos

- Python 3.11 ou superior (`requires-python` em `services/converter/pyproject.toml`).
  A CI usa 3.12.
- Node.js 22 para `apps/mcp` (`engines` do pacote) e Node.js 20 ou superior para
  os demais pacotes (`engines` do `package.json` da raiz). O `.nvmrc` da raiz fixa a
  versão usada pela CI.
- **Tesseract com o idioma `por`** (pacotes `tesseract-ocr` e `tesseract-ocr-por`
  no Debian/Ubuntu). A CI instala os dois antes de rodar o pytest. Localmente, instale-os
  para reproduzir o ambiente da CI. No Windows, se o executável não estiver no
  `PATH`, aponte-o com `TESSERACT_CMD`.
- Dependências Python de teste: `pip install -e ".[test]"` em `services/converter`.
- Ollama com o modelo `glm-ocr:latest` é **opcional**. Sem ele, os testes de GLM-OCR
  reais são pulados (veja [Testando o GLM-OCR](#testando-o-glm-ocr)).

## Corpus de referência

Os PDFs são **gerados** a cada execução por
`services/converter/tests/fixtures/generate_pdfs.py`. Isso mantém o texto e a
geometria exatos conhecidos, sem binários opacos no repositório.

| Fixture | O que exercita | Resultado esperado |
| --- | --- | --- |
| `simple_field` | um campo | aba `Dados`, coluna `Nome` |
| `multi_fields` | vários campos em linhas | 4 colunas, 1 registro |
| `inline_fields` | vários pares na mesma linha | 4 colunas, `001245` como texto |
| `fields_below` | rótulo e valor em linhas separadas | associação correta |
| `multiline_value` | valor em 3 linhas | uma única célula |
| `two_columns` | duas colunas geométricas | 4 colunas, data tipada |
| `bordered_table` | lattice | 3×3 tipada, moeda com formato |
| `borderless_table` | stream/alinhamento | 4×3, colunas separadas |
| `transposed_table` | tabela `Campo \| Valor` | registro com 4 campos |
| `multipage_table` | continuação entre 3 páginas | 6 linhas em uma aba |
| `free_text` | parágrafos | aba `Texto`, sem cabeçalhos inventados |
| `mixed_report` | formulário + tabela + texto | 3 abas |
| `rotated_page` | `/Rotate 90` | texto lido na orientação correta |
| `scanned_page` | sem camada textual | falha explícita (sem OCR instalado) |
| `hybrid_page` | texto + imagem | issue `IMAGE_REGION_NOT_RECOGNIZED` |
| `mixed_native_scan` | página nativa + página só imagem | `partial`, issues, nada inventado |
| `native_plus_blank` | página nativa + página em branco | `validated` (branco não degrada) |
| `encrypted_pdf` | senha | `ENCRYPTED_PDF` |
| `not_a_pdf` | assinatura inválida | `INVALID_FILE` |

## Comandos

| Comando | Pacote | Observação |
| --- | --- | --- |
| `python -m pytest services/converter/tests -q` (ou `npm run test:py` na raiz) | motor Python | a CI roda `pytest tests -q` dentro de `services/converter` |
| `npm --prefix apps/api test` (ou `npm test` na raiz, que também roda a interface) | API NestJS, testes unitários (Jest) | inclui `options.spec.ts` |
| `npm --prefix apps/api run test:e2e` | API NestJS, testes e2e (Jest) | inclui `test/e2e/batches.e2e-spec.ts`. **Não roda na CI.** |
| `npm --prefix apps/web test` | interface (Vitest) | `vitest run` |
| `npm run test:mcp` (ou `npm --prefix apps/mcp test`) | adaptador MCP | compila antes (`pretest`) e roda `dist-test/tests/*.test.js`. **Não roda na CI.** |

No Windows, use `services/converter/.venv\Scripts\python -m pytest tests -q` a
partir de `services/converter`.

### Integração contínua

O workflow `.github/workflows/ci.yml` tem três jobs, em `push` e `pull_request`
para `main`:

| Job | O que roda |
| --- | --- |
| `web` | build dos contratos, build da interface e `npm --prefix apps/web test` |
| `api` | build dos contratos, `db:generate`, typecheck e `npm --prefix apps/api test` |
| `converter` | instala Tesseract e `pip install -e ".[test]"`, depois `pytest tests -q` |

A CI **não tem job para `apps/mcp`**: `npm run test:mcp` não é executado
automaticamente. A CI também não roda os testes e2e da API nem o teste de documentos
grandes (opt-in, veja abaixo). Rode esses comandos localmente antes de mudar
essas áreas.

## Suítes

| Arquivo | Escopo |
| --- | --- |
| `test_normalization.py` | tipos, locale, moeda, identificadores, fórmula |
| `test_label_detection.py` | segmentação rótulo/valor, seções |
| `test_tables.py` | lattice, stream, transposição, continuação |
| `test_pipeline.py` | conversão ponta a ponta por fixture |
| `test_excel.py` | writer + auditor (imagem, fórmula, divergência) |
| `test_api.py` | contrato HTTP, SSE, auth, idempotência, auditoria |
| `test_ocr_glm.py` | GLM-OCR: cliente Ollama, geometria, HTML de tabela, registry |
| `test_audit_fixes.py` | regressões da auditoria: fórmula, cobertura, campos, tabelas, moeda, conciliação, caracteres ilegais, locale |
| `test_consolidated_sheet.py` | aba consolidada `Todos os dados`: união de colunas, proveniência `Aba`/`Página`, ausência de aba duplicada em documento com uma tabela, arquivo único com todas as abas |
| `test_stress_rules.py` | regras reveladas pelo stress: camada de texto invisível, separador decimal por valor, números acima de 15 dígitos como texto, prosa que não é tabela, linha deslocada, tabela sem geometria, precisão do Excel |
| `test_local_app.py` | aplicação local: rotas `/internal` e docs expostas, cookie de sessão `HttpOnly; SameSite=Lax`, `/api/v1/health/ready` sem vazar hosts ou caminhos |

No frontend, `src/lib/api-base.test.ts` cobre a precedência `VITE_API_BASE` > `/api/v1`.

A quantidade de testes muda com o código. Não há contagem fixa neste documento:
a saída dos comandos acima é a referência.

## Suítes de lotes, fila e documentos grandes

Estas suítes cobrem o processamento em lote, a fila durável, os checkpoints, a
exportação TXT/CSV, a imagem como entrada, o conhecimento e o assistente. A coluna
"Comando" é a forma de rodar cada uma.

| Suíte | Comando | Cobre |
| --- | --- | --- |
| Contratos/opções (TS) | `npm --prefix apps/api test` (`options.spec.ts`) | Paridade de opções, rejeição de `max_pages` |
| Paridade (Python) | `pytest tests/test_options_parity.py` | Mesma fixture do lado TS |
| Lotes/upload (Python) | `pytest tests/test_batches_api.py` | 1.001 itens, replay, partes, hash, abort, cancelamento |
| Lotes/upload (NestJS, e2e) | `npm --prefix apps/api run test:e2e` (`batches.e2e-spec.ts`) | Mesmo contrato no modo `full` |
| Fila durável | `pytest tests/test_durable_queue.py` | Fence, lease expirado, cancelamento antes da publicação |
| Checkpoints | `pytest tests/test_page_checkpoints.py` | Retomada sem refazer OCR, invalidação, corrupção |
| Resultado compacto | `pytest tests/test_result_overview.py` | Overview independente do nº de células, paginação de abas |
| UI do resultado | `npm --prefix apps/web test` (`ResultPage.test.tsx`) | Um seletor, uma prévia, parcial/quarentena visíveis |
| Fila no navegador | `npm --prefix apps/web test` (`upload-queue.test.ts`, `BatchPage.test.tsx`) | 1.001 itens sem perda, imagem→TXT, selagem |
| Exportação TXT | `pytest tests/test_text_export.py` | Unicode/emoji, form feed, marcador, CRLF, offsets em code points, tabela sem duplicação, página falha/branca |
| Exportação CSV | `pytest tests/test_csv_export.py` | perfis interoperable/excel_br, aspas, fórmula segura, número negativo, ZIP + manifesto, `NO_TABLES_FOUND` |
| Entrada de imagem | `pytest tests/test_image_input.py` | PNG/JPEG/BMP/GIF/TIFF/WebP, multipágina, EXIF, transparência, limites de quadro/pixel, assinatura |
| Imagem → TXT | `pytest tests/test_image_pipeline.py` | Imagem → TXT com motor simulado; falha explícita sem OCR e sem texto reconhecido |
| Conhecimento | `pytest tests/test_knowledge.py` | Chunks determinísticos, orçamento de tokens, ACL por proprietário e coleção |
| Assistente | `pytest tests/test_assistant.py` | Streaming, citação válida, abstenção sem fonte, modo desativado, falha recuperável |
| Publicação | `pytest tests/test_publication.py` | Aviso publicável, falha fatal em quarentena, qualidade `failed` nunca publica |
| API de exportação | `pytest tests/test_api_upgrade.py` | `/capabilities`, exports TXT/CSV + download, texto paginado, reexport CRLF/marcador, alvo inválido |

### Documentos grandes (opt-in)

```powershell
$env:PDF2X_RUN_LARGE="1"; $env:PDF2X_LARGE_PAGES="10000"
.venv\Scripts\python -m pytest tests/test_large_documents.py -q -s
```

Gera um PDF nativo com marcador único por página e exige: todas as páginas
1..N presentes, zero páginas falhas, ausência de `PAGES_NOT_PROCESSED`, marcador
da última página presente no plano de texto. Sem `PDF2X_RUN_LARGE=1` os testes
são *skipped* — e um skip **não** é aprovação de escala.

O teste com OCR real em 10.000 páginas exige GPU e modelo local. Ele não faz parte
da CI. É uma homologação manual: não há registro versionado dos resultados neste
repositório. Ao executá-la, anote data, máquina, versão do motor e resultado.

## Testando o GLM-OCR

O adapter (`engine/ocr/glmocr.py`) é testado em três níveis:

1. **Cliente e geometria (unitário, sem rede)** — `test_ocr_glm.py` injeta
   fakes de transporte (`poster=`/`getter=`) e cobre: payload enviado ao
   `/api/generate` (modelo, prompt, `temperature=0`, `num_ctx`, `keep_alive`,
   `num_gpu`), remoção de blocos ` thinking`, erros HTTP/serviço/vazio, falha de
   transporte → `OcrUnavailable`, seleção e cache do probe, precedência no
   registry, projeção de linhas, distribuição ancorada na tinta, parsing do
   HTML de tabela e recusa de exportar marcação crua.
2. **Integração real** — `TestRealGlmOcrIntegration` conversa com o Ollama
   local; é **automaticamente pulado** (`skipif`) quando o serviço ou o modelo
   não estão presentes, para a suíte não depender de GPU.
3. **Ponta a ponta** — `test_scanned_page_is_recovered_by_glm_ocr` exige que a
   página sem camada de texto volte como `text_source="ocr"` e que o evento
   `OCR_COMPLETED` registre `engine`, `model` e `boxing_method`. Este teste precisa
   de um Ollama acessível com `glm-ocr:latest`.

Os testes de degradação (`no_engine=True` em `run_fixture`) **simulam a
ausência de motor** em vez de depender do que está instalado na máquina — assim
o caminho "falha sem inventar" continua verificável mesmo com o GLM-OCR ativo.

## O que cada camada garante

**Motor**

- toda fixture válida gera XLSX **sem** `xl/media/`, sem desenho e sem fórmula;
- toda célula planejada corresponde ao valor escrito;
- todo bloco de texto tem destino (`destination is not None`);
- a cadeia de hash verifica;
- páginas digitalizadas/criptografadas falham sem inventar dados;
- página sem texto recuperável → `partial` + `unrecognized_regions` (warn);
  página em branco legítima **não** degrada a qualidade;
- linha de campo (`Nome: Fulano Exemplo`) nunca é tratada como título de seção;
- linha de dados (`Ciclano Exemplo 29/01/2025`) nunca é tratada como título
  de seção;
- cabeçalho de tabela desenhado sem régua interna é redistribuído por coluna;
- número grudado na célula vizinha (`Treinamento 2` + Qtd vazio) é separado;
- texto com modo de renderização invisível (`3 Tr`) não é confundido com texto
  visível: com imagem, o OCR visual prevalece; sem imagem, o texto é preservado
  e sinalizado;
- valores de AcroForm viram campos do workbook (rótulo visual recuperado);
- a cópia temporária criada para normalizar `/Rotate` é removida ao final.

**API**

- upload inválido → 400; conversão alheia → 403; duplicata idempotente → mesmo id;
- `download` antes do fim → 409;
- SSE entrega eventos com `id` e permite retomada;
- auditoria expõe eventos, página, células e exportação JSON/TXT;
- rotas internas exigem token.

**Frontend**

- validação de arquivo, navegação, 12 abas de auditoria, parsing de erro,
  retomada de SSE.

## Como adicionar um caso

1. Escreva um builder em `generate_pdfs.py` com geometria determinística.
2. Registre em `BUILDERS`.
3. Adicione a expectativa em `test_pipeline.py` (abas, cabeçalhos, valores).
4. Rode `pytest -q` e depois o smoke visual:

```bash
cd services/converter
.venv/Scripts/python scripts/smoke_run.py nome_da_fixture
```

## Métricas que importam

- **cobertura textual**: blocos recuperados com destino documentado (100% nas
  fixtures).
- **precisão**: medida contra o gabarito do corpus de stress, descrito na seção
  [Stress contra o corpus dourado](#stress-contra-o-corpus-dourado-3208-pdfs).
  As fixtures são um gabarito parcial (valores exatos verificados nos testes).
  Cobertura textual não é prova de precisão.
- **desempenho**: `duration_ms` por execução e por fase são persistidos.

## Não coberto por automação aqui

- Testes de navegador automatizados: há smoke manual (upload → resultado →
  auditoria → assistente) contra o backend real. A compilação TypeScript/Vite e
  a suíte Testing Library cobrem os componentes e a regressão de tipos.
- `docker compose up`: não há execução registrada da composição neste
  repositório. Ela foi escrita e revisada; os modos sem container foram executados.
- OCR real: o GLM-OCR depende de um Ollama local com `glm-ocr:latest` e, para a
  velocidade medida em [CONVERSION.md](CONVERSION.md), de GPU. Sem isso, o adapter
  é coberto pelos testes unitários; a qualidade do OCR só é medida pelo stress, que
  depende do corpus local.

## Stress contra o corpus dourado (3208 PDFs)

O corpus de stress é **local**: a pasta `stress/pdfs` **não faz parte do
repositório** (ela está no `.gitignore`, em `/stress/`). Sem essa pasta, o
harness não roda; os testes do repositório não dependem dela, porque
`test_stress_rules.py` pina regras gerais do motor, não arquivos do corpus.

O corpus tem 9 packs e 64 categorias. Cada pack traz um `GABARITO.jsonl`, que
declara, para cada PDF, o conteúdo lógico que **deve** sair (campos e linhas de
tabela) e o tratamento esperado nos casos negativos (criptografado, corrompido,
vazio, texto invisível, camada de texto não confiável).

`services/converter/scripts/stress_gabarito.py` converte cada PDF de
`stress/pdfs` pelo pipeline real e **pontua** o resultado contra o gabarito. Não
apenas executa: cada documento recebe uma nota por métrica.

| Métrica | Como é medida |
| --- | --- |
| `expectation_ok` | a classe do caso foi tratada corretamente (tabela abaixo) |
| `fields.ratio` | valores esperados encontrados **na coluna certa** |
| `fields.extraction_ratio` | valores encontrados em qualquer coluna (extração ok, estrutura errada) |
| `table.row_ratio` / `cell_ratio` | linhas/células esperadas reproduzidas |
| `absent_from_source` | valores declarados que o PDF **não imprime** (gabarito genérico) — fora do denominador |
| `not_in_source` | tabela esperada que o documento não imprime — não pontuada |

Fair-play contra falsos defeitos: o conteúdo real do arquivo (camada textual +
valores de AcroForm) é consultado antes de exigir um valor. O que não está no
arquivo não é cobrado do conversor.

| `expectativa` | Aprova quando |
| --- | --- |
| `dados_visiveis` | campos ≥ 99% na coluna certa e tabela ≥ 90% (quando impressa) |
| `camada_de_texto_nao_confiavel` | idem — os valores visuais (OCR) prevalecem sobre a camada oculta |
| `texto_nao_visivel` | idem **e** issue `INVISIBLE_TEXT_LAYER` registrada |
| `pdf_sem_dados` | falha `NO_CONTENT`, zero dado inventado |
| `solicitar_senha_para_extrair` | falha `ENCRYPTED_PDF` |
| `tratamento_de_pdf_corrompido` | falha controlada, sem exceção |

### Comandos

Execute a partir de `services/converter` (no Windows, `.venv\Scripts\python`):

```bash
# extração nativa (rápido)
.venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --order asc

# digitalizados + híbridos, retomável e com orçamento de tempo
.venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --ocr --budget 3600

# triagem rápida: 2 documentos por categoria
.venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --per-category 2

# amostra: os 10 primeiros documentos, com os três formatos de saída
.venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --limit 10 --formats xlsx,txt,csv
```

| Flag | Função | Padrão |
| --- | --- | --- |
| `--root CAMINHO` | pasta com os packs de PDF e os `GABARITO.jsonl` | `stress/pdfs` na raiz do repositório |
| `--order asc\|desc\|name` | ordem de execução; `asc` exercita os documentos leves antes dos pesados | `asc` |
| `--ocr` | inclui documentos digitalizados e híbridos (OCR) | desligado |
| `--only-ocr` | processa apenas os documentos que o gabarito marca como OCR obrigatório | desligado |
| `--per-category N` | limita a N documentos por categoria (triagem) | sem limite |
| `--limit N` | processa apenas os N primeiros documentos depois dos filtros | `0` (sem limite) |
| `--categories A,B` | filtra por trecho do nome do arquivo ou da pasta da categoria (lista separada por vírgula) | todas |
| `--formats A,B` | formatos de saída gerados em cada conversão (`xlsx`, `txt`, `csv`) | `xlsx,txt` |
| `--max-pages N` | valor repassado ao pipeline. **Sem efeito na prática:** o pipeline de documento inteiro ignora o teto (veja [CONVERSION.md](CONVERSION.md#documentos-grandes-sem-teto-de-páginas)) | `500` |
| `--budget SEGUNDOS` | orçamento de tempo da execução; `0` = sem orçamento | `0` |
| `--live` / `--no-live` | imprime cada marco de página durante a conversão | `--live` |
| `--state CAMINHO` | arquivo de estado retomável | `services/converter/data/stress_gabarito.json` |
| `--report CAMINHO` | relatório agregado, reescrito a cada documento | `services/converter/data/stress_gabarito_report.json` |
| `--rerun` | ignora resultados já gravados no estado | desligado |

O estado é retomável (`--state`) e o relatório (`--report`) é reescrito a cada
documento, com agregados por categoria, falhas nomeadas e códigos de issue. Uma
execução interrompida continua de onde parou. Use `--rerun` para refazer tudo.

### Resultados medidos

Resultados medidos em 2026-11-16 (corpus local de 3208 PDFs; OCR em amostra
estratificada: 5 documentos por categoria digitalizada).

| Grupo | Docs | Aprovação | Campos | Tabelas |
| --- | --- | --- | --- | --- |
| nativo | 1400 | 98,1% | 100,0% | 99,6% |
| extremo (vazio/cripto/corrompido/invisível) | 300 | 100% | — | — |
| híbrido, sem OCR | 500 | 60,0% | 85,7% | 100% |
| digitalizado, sem OCR | 1000 | 0% (esperado) | — | — |
| **todos, sem OCR** | 3208 | **61,7%** | 61,7% | 50,9% |
| **quem não depende de OCR** | 1700 | **98,5%** | 93,9% | 91,3% |

Sem OCR, antes → depois: expectativa geral **58,4% → 61,7%**; híbrido **35% → 60%**
(valores atuais na tabela acima).

Com OCR — amostra estratificada (100 docs, 5 por categoria digitalizada):

| Métrica | Antes | Depois |
| --- | --- | --- |
| Aprovação | 40% | 57% |
| Campos | 92,9% | 93,5% |
| Tabelas | 54% | 70,7% |
| Categorias 5/5 com tabela 100% | 2 | 6 |

As seis categorias que chegaram a 100% de tabela são: baixo contraste, rotação
180°, sombra lateral, texto miúdo, desbotado e binarizado.

O pass completo com OCR (1000 docs, ~2 h) **não foi executado integralmente**;
os números com OCR vêm apenas da amostra estratificada acima.

### Limitações da medição

- Campos são o ponto forte. As tabelas caem exatamente nas classes em que a
  tinta não segmenta (desfocado, sombra lateral, texto miúdo, binarizado,
  resolução abaixo de ~6 px por glifo).
- Nessas classes, a tabela é reconstruída pelo padrão do texto quando o grid é
  limpo; quando não é, o resultado fica registrado como parcial em vez de
  inventado.
- O resultado com OCR vem de **amostra**, não do corpus inteiro.
- Os números dependem do corpus local, que não é versionado: reproduzir a
  medição exige o mesmo corpus.
