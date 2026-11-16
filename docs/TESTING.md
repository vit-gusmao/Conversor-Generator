# Estratégia de testes

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
| `transposed_table` | tabela `Campo | Valor` | registro com 4 campos |
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

A quantidade de testes muda com o código. A saída dos comandos abaixo é a
referência:

| Comando | Pacote |
| --- | --- |
| `python -m pytest services/converter/tests -q` | motor Python |
| `npm --prefix apps/api test` | API NestJS |
| `npm --prefix apps/web test` | interface |

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
   `OCR_COMPLETED` registre `engine`, `model` e `boxing_method`.

Os testes de degradação (`no_engine=True` em `run_fixture`) **simulam a
ausência de motor** em vez de depender do que está instalado na máquina — assim
o caminho "falha sem inventar" continua verificável mesmo com o GLM-OCR ativo.

## O que cada camada garante

**Motor**

- toda fixture válida gera XLSX **sem** `xl/media/`, sem desenho e sem fórmula;
- toda célula planejada corresponde ao valor escrito;
- toda bloco de texto tem destino (`destination is not None`);
- a cadeia de hash verifica;
- páginas digitalizadas/criptografadas falham sem inventar dados;
- página sem texto recuperável → `partial` + `unrecognized_regions` (warn);
  página em branco legítima **não** degrada a qualidade;
- linha de campo (`Nome: João`) nunca é tratada como título de seção;
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
- **precisão**: comparação com gabarito. As fixtures funcionam como gabarito
  parcial (valores exatos verificados nos testes); um conjunto dourado maior é
  a evolução recomendada.
- **desempenho**: `duration_ms` por execução e por fase são persistidos.

## Suítes do upgrade

| Arquivo | Cobertura |
| --- | --- |
| `tests/test_text_export.py` | Unicode/emoji, form feed, marcador, CRLF, offsets em code points, tabela sem duplicação, página falha/branca |
| `tests/test_csv_export.py` | perfis interoperable/excel_br, aspas, fórmula segura, número negativo, ZIP + manifesto, `NO_TABLES_FOUND` |
| `tests/test_image_input.py` | PNG/JPEG/BMP/GIF/TIFF/WebP, multipágina, EXIF, transparência, limites de quadro/pixel, assinatura |
| `tests/test_image_pipeline.py` | imagem → TXT com motor simulado; falha explícita sem OCR e sem texto reconhecido |
| `tests/test_knowledge.py` | chunks determinísticos, orçamento de tokens, ACL por proprietário e coleção |
| `tests/test_assistant.py` | streaming, citação válida, abstenção sem fonte, modo desativado, falha recuperável |
| `tests/test_publication.py` | aviso publicável, falha fatal em quarentena, qualidade `failed` nunca publica |
| `tests/test_api_upgrade.py` | `/capabilities`, exports TXT/CSV + download, texto paginado, reexport CRLF/marcador, alvo inválido |

## Não coberto por automação aqui

- Testes de navegador automatizados: há smoke manual (upload → resultado →
  auditoria → assistente) contra o backend real. A compilação TypeScript/Vite e
  a suíte Testing Library cobrem os componentes e a regressão de tipos.
- `docker compose up`: o ambiente de construção não possui Docker. A composição
  foi escrita e revisada; os modos sem container foram executados.
- OCR real: **GLM-OCR está instalado** (Ollama, RTX 2070) e foi exercitado pelos
  testes de documentos reais. Ainda não há benchmark de precisão por classe no
  corpus dourado — cobertura de texto não é prova de precisão.
