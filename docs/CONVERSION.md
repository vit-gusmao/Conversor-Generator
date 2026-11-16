# O motor de conversão

Este documento descreve o que acontece entre o upload e o `.xlsx`, fase por
fase, com o módulo responsável e o critério de qualidade aplicado.

## Fase 01 — Receber e validar

`engine/inspector/pdf_inspector.py`

- assinatura `%PDF-` (com tolerância a lixo inicial);
- abertura por parser (`pypdf`), contagem de páginas;
- criptografia: tenta senha de usuário vazia; se falhar, `ENCRYPTED_PDF`;
- tamanho e número de páginas contra os limites configurados;
- conteúdo ativo **detectado e ignorado**: JavaScript, `/Launch`,
  `/OpenAction`, arquivos embutidos, XFA;
- formulários AcroForm: nomes internos + rótulo visual + geometria.

Nada do PDF é executado. O relatório de validação acompanha a tarefa e é
mostrado na interface.

## Fase 02 — Inspecionar páginas e regiões

`engine/extraction/native_text.py`, `engine/layout/regions.py`

Classificação **por página** (nunca do documento inteiro): `native`, `scan`,
`hybrid`, `image`, `empty`. Cada página informa fonte de texto, caracteres
nativos, caracteres de OCR, imagens e cobertura de imagem.

`/Rotate` é resolvido antes de qualquer extração: pypdf aplica a rotação ao
conteúdo (`transfer_rotation_to_content`) e o motor trabalha em coordenadas
visuais. Isso elimina uma classe inteira de bugs de ordenação.

## Fase 03 — Extrair texto nativo

- palavras com `bbox`, fonte, tamanho, direção, página e motor;
- qualidade da camada textual: caracteres de substituição, caracteres de
  controle, texto invisível (cor branca), fonte minúscula, palavras coladas,
  duplicação por sobreposição;
- `score < 0.55` dispara a rota de OCR seletivo.

## Fase 04 — OCR (somente quando necessário)

`engine/ocr/*`

1. rasterização por `pypdfium2` (DPI configurável, com limite de pixels);
2. orientação: a projeção de tinta indica página deitada; as duas direções
   (90°/270°) são lidas e vence a que produz **geometria medida** (faixas de tinta
   1:1 com as linhas reconhecidas). A rotação corrige as coordenadas de volta ao
   espaço da página original — girar a imagem sem remapear colocava cada palavra
   fora da página. Sem geometria medida, a página é lida na orientação original;
3. pré-processamento adaptativo com OpenCV — deskew, CLAHE, denoise,
   limiarização adaptativa — sempre preservando a imagem original. O raster
   **original** é o que vai ao motor (a geometria depende dele); a versão
   melhorada é apenas fallback de reconhecimento;
4. motor, na ordem de preferência: **GLM-OCR** (VLM local via Ollama, GPU) →
   RapidOCR → Tesseract (`--psm 6` página, `--psm 7` linha/célula);
5. geometria: as faixas de linha saem da projeção de tinta; a posição x de cada
   palavra sai da **tinta da própria linha** (merge/split de trechos quando a
   contagem divergir). Sem isso a linha era distribuída por proporção e as
   colunas nunca alinhavam;
6. coordenadas convertidas de pixels para pontos;
7. reconciliação: palavra de OCR que duplica texto nativo no mesmo lugar é
   descartada; texto nativo válido nunca é substituído por OCR — exceto quando a
   camada nativa é **invisível** (`3 Tr`/`7 Tr`) e há imagem: aí o OCR visual
   prevalece, porque é o que o leitor vê;
8. **sem motor instalado**, a região é registrada como não reconhecida e a
   página falha de forma explícita. Nenhum caractere é inventado;
9. legibilidade: glifos com menos de ~6 px de altura geram a issue
   `OCR_LOW_LEGIBILITY` — o texto é exportado, mas a auditoria diz que a fonte
   não era legível o suficiente para confiar palavra por palavra.

### Grid que é formulário, não tabela

Um bloco de texto em colunas com vários `Rótulo:` dentro das células não vira
tabela: a extração de campos nunca veria essas linhas (elas estariam "dentro de
uma tabela") e o formulário inteiro viraria uma célula falsa. O teste é direto —
duas células com dois ou mais marcadores de rótulo reprovam o candidato — e o
texto segue para campos/aba `Texto`.

### Linhas idênticas repetidas

O mesmo documento digitalizado em duas páginas (uma nativa, uma raster) gerava
a mesma entidade três vezes. Linhas **idênticas em todas as colunas** nas abas
de registro são colapsadas e o fato é registrado (`DUPLICATE_ROWS_REMOVED`);
tabelas nunca passam por essa regra, porque itens repetidos são legítimos.

### Tabelas quando a geometria não serve

`engine/tables/text_pattern.py` reconstrói a tabela **pelo texto**, sem
coordenadas: uma linha de cabeçalho com ≥3 rótulos conhecidos (`Item`,
`Descrição`, `Qtd`, `Preço`, `Total`) seguida de ≥2 linhas que terminam em
números. Só entra quando nenhum detector geométrico produz candidato (scan
desfocado, banda de tinta única) e cada linha mantém sua própria caixa como
proveniência. Valores não são reescritos: só as fronteiras de célula são
inferidas.

### Motor GLM-OCR

`engine/ocr/glmocr.py` integra o modelo `glm-ocr:latest` servido pelo Ollama
(endpoint nativo `/api/generate`, o recomendado pelo repositório oficial). O
processamento é **100% local e na GPU**: o Ollama carrega todas as camadas
quando há VRAM (`ollama ps` mostra `100% GPU`).

| Aspecto | Decisão |
| --- | --- |
| Seleção | automática quando `GLMOCR_ENABLED=true` e o modelo responde; `CONVERTER_OCR_ENGINE=glm-ocr` força, `rapidocr`/`tesseract` excluem |
| Prompt | `Text Recognition:` (padrão), `Table Recognition:` (regiões de tabela), `Formula Recognition:` |
| Determinismo | `temperature=0`, `num_ctx=8192` (evita truncar páginas densas), `keep_alive=30m` (mantém o modelo quente) |
| Geometria | `projection_v1`: linhas por projeção de tinta (NumPy) casadas 1:1 com as linhas do modelo; `even_distribution`: distribuição ancorada na extensão de tinta quando a contagem diverge; `html_grid_v1`: grade inferida a partir do HTML de tabela |
| Tabelas | a resposta `Table Recognition:` vem em HTML; é convertida em uma grade de células (uma por célula, com posição de linha/coluna) e **nunca** exportada como marcação crua |
| Confiança | constante (`constant_vlm`), pois o VLM não devolve score por token — registrada como tal, não como precisão medida |
| Auditoria | `OCR_STARTED`/`OCR_COMPLETED` carregam `model`, `api`, `num_ctx`, `gpu`, `boxing_method`, `confidence_policy`; o manifesto lista `glm-ocr` na tabela de motores |

Desempenho medido neste ambiente (página A4 a 300 DPI, modelo já carregado):
~0,7 s por página. A primeira chamada após o carregamento leva alguns segundos
a mais.

Se o serviço cair no meio da conversão, o motor levanta `OcrUnavailable` e a
página é marcada como não recuperada — a conversão não inventa texto nem trava
o pipeline.

### PDFs só de imagens

A decisão é por página, não por documento:

| Situação | Resultado |
| --- | --- |
| documento inteiro só com imagens, sem OCR | `failed` / `NO_CONTENT` |
| documento misto (página nativa + página escaneada) | `completed_with_warnings` / `partial` |
| página sem texto em documento misto | `status="partial"`, `text_source="none"` |

Issues registradas: `OCR_UNAVAILABLE`, `IMAGE_REGION_NOT_RECOGNIZED`,
`PAGE_WITHOUT_TEXT` e a checagem `unrecognized_regions`. A imagem é usada
apenas como fonte temporária de leitura; o XLSX continua sem qualquer mídia.

**Limitação atual:** o OCR por célula/linha (`OcrRunner.run_region`,
`--psm 7`) existe e está testado no nível do componente, mas o pipeline chama
apenas OCR de página. O refinamento célula a célula após a detecção da grade
ainda não está conectado.

## Fase 05 — Compreender o layout

`engine/layout/blocks.py`, `reading_order.py`

- palavras → linhas (proximidade vertical + sobreposição);
- linhas → blocos (espaçamento, alinhamento horizontal, mudança de tamanho de
  fonte);
- ordem de leitura por **XY-cut recursivo**: detecta rios de espaço, separa
  colunas visuais, ordena da esquerda para a direita.

Coluna visual ≠ coluna semântica. A primeira serve à leitura; a segunda nasce
dos campos e tabelas.

## Fase 06 — Detectar campos e valores

`engine/fields/*`

Quatro passagens, nesta ordem de confiança:

| Passagem | Entrada | Resultado |
| --- | --- | --- |
| 1 | `Nome: Fulano Exemplo` (um ou vários pares por linha) | coluna `Nome` |
| 2 | `Nome:` e o valor na linha seguinte (inclusive multilinha) | coluna `Nome` |
| 3 | duas colunas geométricas, sem separador | par rótulo/valor |
| 4 | campos AcroForm | valor direto + rótulo visual |

Heurísticas usadas: separador (`:`/`=`), transição de fonte (negrito), gap
horizontal, alinhamento consistente, forma do rótulo (1–8 palavras, sem
dígitos), conectores (`de`, `da`, `do`).

Valores multilinha permanecem em **uma célula**: `Endereço: Avenida Principal,`
+ `número 250, bloco B,` + `apartamento 301`.

Agrupamento em registros (`record_grouper.py`): salto vertical anormal, mudança
de seção e título intermediário quebram o registro, e a **repetição do rótulo
que abriu o registro** indica reinício (`Nome, Cargo, Nome, Cargo` →
`Funcionário 1 / Funcionário 2` gera duas linhas, não quatro colunas). Um rótulo
que apenas repete dentro do mesmo registro (`Telefone` duas vezes) **não** o
quebra: os dois valores seguem juntos e viram `Telefone` e `Telefone (2)`.

## Fase 07 — Reconstruir tabelas

`engine/tables/*`

- **lattice** (bordas): `pdfplumber` com `lines` + fallback próprio que une
  arestas, calcula interseções e monta a grade;
- **stream** (sem bordas): cada linha é dividida em segmentos por gaps largos;
  segmentos são agrupados em colunas por sobreposição/alinhamento — funciona
  para texto à esquerda e números à direita;
- **híbrido**: réguas horizontais reais definem linhas, alinhamento define
  colunas;
- **Docling** (opcional): estrutura apenas; os caracteres continuam vindo do
  texto nativo/OCR.

Pós-processamento: cabeçalho por sinal numérico ou negrito, células mescladas,
linhas de continuação (não criar um funcionário chamado `Dados`), linhas de
total (`summary`), tabela transposta (duas colunas de rótulos → um registro).

O sinal de "documento de formulário" no algoritmo `stream` é a **proporção** de
linhas com rótulo (`Nome:`), não a existência de uma: uma tabela com uma linha
`Total:` continua sendo tabela. As mesclagens são detectadas na grade final
(depois dos recortes de borda) e ficam como **metadado** da tabela — o XLSX é
sempre normalizado, uma informação por célula.

## Fase 08 — Validar candidatos

`engine/tables/table_builder.py` + `engine/validation/*`

Cada candidato recebe pontuação por cobertura de texto, densidade, tamanho e
estratégia. Sobreposição é resolvida por IoU/containment: o melhor fica, os
outros viram **alternativas registradas** na auditoria (nunca são descartados em
silêncio).

## Fase 09 — Normalizar

`engine/normalization/normalizer.py`

Locale inferido do documento (separador decimal, milhar, ordem de data).
Tipos: texto, inteiro, decimal, moeda, data, percentual, competência,
identificador, código, booleano, vazio.

- moeda com `Decimal`; o **símbolo do documento é preservado** (`R$`, `US$`,
  `€`, `£`) tanto na exibição quanto no formato da célula;
- separador decidido **por valor**, não pelo locale do documento: um mesmo PDF
  pode imprimir preços com ponto (`308.36`) e um campo de valor com vírgula
  (`R$ 153,18`). Confiar só no locale transformava `153,18` em `15318`;
- números com mais de **15 dígitos significativos** ficam como texto (`@`):
  o Excel arredonda acima disso, e a célula deixaria de corresponder ao plano
  (o artefato era reprovado na própria auditoria);
- quando o cabeçalho indica valor (`Salário`, `Total`, `Valor`) e a célula traz
  apenas o número, o valor é tratado como moeda mesmo sem símbolo;
- identificadores (CPF, CNPJ, CEP, matrícula, telefone) sempre texto;
- zeros à esquerda preservados (`001245`);
- `(350,00)` contábil → negativo;
- datas ambíguas (dia e mês ≤ 12) são aceitas com confiança menor e a
  suposição de ordem (`dmy`/`mdy`) fica registrada na transformação;
- texto iniciado por `=` permanece dado, nunca fórmula (garantido na escrita);
- caracteres de controle que o XLSX não aceita são substituídos por `U+FFFD` e
  registrados (`ILLEGAL_CHARACTERS_REPLACED`), em vez de abortar a geração;
- a aba `Texto` guarda texto **literal**: um parágrafo composto só de dígitos
  não vira número;
- o valor original acompanha o normalizado em toda célula.

## Fase 10 — Conciliar páginas

`engine/reconciliation/merger.py`

Tabelas continuadas são unificadas quando há evidência combinada: mesma
quantidade de colunas, cabeçalhos compatíveis, tipos compatíveis, geometria
próxima, páginas adjacentes. Cabeçalho repetido é descartado. Sem evidência
suficiente, as tabelas ficam separadas — unir errado é pior que duas abas.
A primeira linha da página seguinte só é descartada quando **repete o
cabeçalho**; uma linha de dados que por acaso coincide com a última da página
anterior é preservada.

Registros duplicados (mesma página, mesma assinatura, bbox sobreposto) são
removidos e o fato é registrado.

### Detecção de tabelas em nível de página

Tabelas são estruturas da página, não de um bloco de layout: um bloco
intermediário pode cortar a tabela ao meio (cabeçalho num bloco, itens em
outro, totais num terceiro). Por isso o detector `stream` roda sobre **todos os
tokens da página** e ele mesmo separa regiões:

- faixas verticais separam tabelas diferentes (vão maior que ~2 linhas);
- uma **régua desenhada** dentro do vão mantém as duas partes unidas — o layout
  "itens acima, totais abaixo" tem exatamente esse formato;
- grades montadas a partir da **moldura da página** (retângulo de fundo maior
  que a página, comum em PDFs gerados de HTML) são descartadas: células com
  altura de dezenas de pontos são moldura, não tabela;
- prosa em duas colunas visuais (bloco de endereço, artigo) não é tabela: sem
  coluna numérica e com células longas, o texto segue para campos/aba `Texto`.

Uma linha só é fundida na anterior como continuação quando tem **um único valor**
alinhado sob uma coluna que não é a primeira (`Analista de` / `Dados`). Uma linha
com dois ou mais valores é um registro próprio — é o que impede `Subtotal`,
`Total` e `Amount due` de virarem uma linha só.

### Regra de não-perda por linha

Um bloco de texto pode conter, ao mesmo tempo, um campo usado e linhas que não
viraram célula. O consumo é decidido **linha a linha**, comparando coordenadas
com as caixas das células já preenchidas: as linhas não usadas vão para a aba
`Texto` em vez de desaparecerem com o bloco. Isso evita tanto a perda silenciosa
quanto a duplicação do que já foi estruturado.

## Fase 11 — Montar o workbook

`engine/structure/resolver.py`

- campos → colunas (ordem de primeira aparição, cabeçalhos dinâmicos);
- rótulos repetidos → `Telefone 1`, `Telefone 2`;
- registros → linhas; tabelas independentes → abas; tabela transposta →
  registro;
- texto livre → aba `Texto` (`Texto`, `Seção`, `Página`);
- conteúdo sem estrutura → aba `Não classificado`;
- **aba consolidada** `Todos os dados`: quando existem duas ou mais abas
  estruturais (registros/tabelas), uma aba extra repete cada linha com a
  **união de todas as colunas** e duas colunas de origem (`Aba`, `Página`).
  É uma cópia do plano — nunca uma nova extração —, então não pode divergir
  das abas originais; o download continua sendo **um único `.xlsx`**;
- limites do Excel respeitados por divisão, nunca por truncamento.

## Fase 12 — Gerar o XLSX

`engine/excel/writer.py`

Cabeçalho estilizado, congelamento, autofiltro, larguras, formatos numéricos.
Identificadores com formato `@`. Nenhuma imagem. Texto acima de 32.767
caracteres é truncado com registro.

## Fase 13 — Auditar o arquivo

`engine/excel/auditor.py`

Reabre o pacote e verifica: ZIP íntegro, partes obrigatórias, **zero**
`xl/media/`, zero relacionamento de imagem, zero objeto de imagem, zero
fórmula, abas e valores idênticos ao plano.

## Fase 14 — Publicar

XLSX e manifesto comprimido vão para o storage; o banco guarda o mapa completo
de células e evidências. O download só é liberado após a auditoria passar.

## Fase 15 — (Opcional) Formulários interativos

Campos AcroForm fornecem valor + geometria; o rótulo visual é recuperado pela
posição do widget, evitando nomes internos como `TextField12`.

## Fase 16 — Falhar bem

`ConversionError` carrega código, mensagem, retryability e detalhe. Os códigos
terminais: `INVALID_FILE`, `ENCRYPTED_PDF`, `NO_CONTENT`, `INTERNAL_ERROR`,
`CANCELLED`. Uma página com problema não derruba o documento: ela é marcada
`failed` e o restante continua.

## Escada de fallback (com limite)

```
extração nativa
   └─ cobertura < 100% ou qualidade baixa
        └─ OCR de página
             └─ Docling (se instalado)
                  └─ preservação textual + issue
```

O OCR por região (`run_region`) está disponível no componente, mas ainda não é
acionado automaticamente pelo pipeline — por isso não aparece como degrau
autônomo acima.

Cada degrau tem limite de tentativas, tempo e memória
(`CONVERTER_MAX_FALLBACK_ATTEMPTS`, `CONVERTER_TASK_TIMEOUT_SECONDS`). O sistema
nunca entra em laço infinito: quando não há confiança, preserva o texto e
registra a pendência.

## Exportadores TXT e CSV

O mesmo IR alimenta todos os artefatos de uma execução: o OCR roda uma vez e
`text_plan.json.gz` guarda os segmentos textuais por página.

**TXT** (`exports/text_plan.py`, `exports/text_writer.py`)

- ordem de leitura por blocos; tabelas renderizadas como linhas separadas por
  tabulação e nunca duplicadas com o bloco que as originou;
- UTF-8 sem BOM e LF por padrão; CRLF e marcador `--- Página N ---` são opções;
- form feed (`\f`) separa páginas; offsets em pontos de código Unicode;
- página falha não vira página branca: aparece no manifesto como falha;
- nenhum título, resumo ou texto de modelo é inserido no arquivo.

**CSV** (`exports/csv_writer.py`)

- uma tabela selecionada → `.csv`; várias → `.zip` com um CSV por tabela e
  `manifest.json`; nenhuma tabela confiável → `NO_TABLES_FOUND` (ofereça TXT);
- perfis `interoperable` (UTF-8, vírgula, CRLF) e `excel_br` (BOM, ponto e
  vírgula);
- `formula_policy=safe` prefixa apóstrofo em campos textuais que começariam com
  `=`, `+`, `-`, `@` ou caractere de controle, sem alterar números negativos;
- o CSV não promete tipos do XLSX; zeros iniciais podem ser reinterpretados
  pelo Excel, e o manifesto explica.

## Imagens → TXT

`ingestion/image_input.py` detecta a assinatura real (não a extensão), decodifica
com Pillow, aplica EXIF **uma vez**, compõe transparência em branco e converte
para RGB. TIFF/GIF multipágina produzem um quadro por página, com limite de
500 quadros, 40 MP por quadro e 200 MP por job (configuráveis).

`ingestion/image_pipeline.py` usa o mesmo pré-processamento, motores de OCR e
layout do PDF. Sem motor de OCR a execução falha com `OCR_UNAVAILABLE`; com OCR
e nenhum texto acima da confiança, falha com `NO_TEXT_RECOVERED`. Nunca há
“conversão concluída” com arquivo vazio.

## Gate de publicação

`engine/validation/publication.py` separa falhas fatais de avisos:

- **fatal**: ZIP corrompido, partes obrigatórias ausentes, mídia incorporada,
  fórmula, aba/célula divergente do plano, limite do formato excedido;
- **aviso**: cobertura parcial, página sem texto, truncamento de célula.

Falha fatal → `publication_status=quarantined`, `status=failed` e download
bloqueado nas duas APIs. Aviso permite download informado. O manifesto registra
a versão da política e as regras acionadas.
