# Auditoria

A auditoria não é um log de mensagens: é o **registro de decisões** que
explica por que cada célula existe.

## Três níveis

| Nível | O que registra | Onde |
| --- | --- | --- |
| Operacional | início/fim/falha de cada fase, retentativas | `audit_events`, `stage_timings` |
| Estrutural | o que foi detectado em cada página, região, campo e tabela | `page_results`, `regions`, `text_elements`, `extracted_*` |
| Proveniência | de onde veio cada valor e para qual célula foi | `output_cells`, `cell_evidence`, `decisions` |

## Evento

```json
{
  "event_id": "evt_000184",
  "run_id": "run_...",
  "sequence": 184,
  "event_type": "FIELD_MATCH_ACCEPTED",
  "stage": "field_resolution",
  "severity": "info",
  "page_number": 1,
  "region_id": null,
  "extraction_method": "native",
  "message": "Nome -> accepted (field-resolver-v1)",
  "payload": {
    "decision_id": "dec_000042",
    "reason_codes": ["same_line", "colon_separator", "label_at_line_start"],
    "label": "Nome",
    "value": "João da Silva",
    "label_bbox": [72.0, 91.28, 105.2, 102.28]
  },
  "previous_hash": "...",
  "event_hash": "..."
}
```

## Cadeia de hash

```
event_hash = sha256( payload_hash(sequence, type, stage, page, region, payload,
                                  severity, message, extraction_method, created_at)
                     | previous_hash )
```

O corpo coberto inclui a **mensagem legível**, a **severidade**, o **método de
extração** e o **instante** (em UTC sem fuso, para sobreviver ao round-trip do
banco): editar o texto que descreve uma decisão invalida a cadeia tal como
editar um dado. Cadeias gravadas por versões anteriores (que cobriam só
`seq/type/stage/page/region/payload`) continuam verificáveis — a verificação
aceita as duas formas.

`sequence` é monotônico por execução. Qualquer alteração em um evento invalida
todos os seguintes — `/audit` reporta `hash_chain_valid`. Um atacante com acesso
ao banco pode reescrever a cadeia inteira; por isso a recomendação de produção é
publicar checkpoints assinados fora do banco.

## Cobertura × precisão

```
cobertura = tokens recuperados com destino documentado / tokens recuperados
```

Destinos possíveis: célula (campo/tabela), texto preservado, metadado, conteúdo
repetitivo (cabeçalho/rodapé) ou conteúdo não classificado preservado em aba
própria. **Um bloco sem destino nenhum conta como perdido** (`dropped`) e reduz
a cobertura: 100% só acontece quando todo texto recuperado chegou a algum
destino.

A cobertura é calculada por página e agregada. **Cobertura de 100% não significa
acerto de 100%**: OCR pode errar caracteres. Por isso o manifesto expõe
`measured_precision: null` a menos que um conjunto de referência seja fornecido.

### Caracteres que o XLSX não aceita

XML 1.0 proíbe caracteres de controle (`0x00`–`0x08`, `0x0B`, `0x0C`,
`0x0E`–`0x1F`). PDFs reais contêm esses bytes — inclusive usados como separador
de campo em faturas geradas por sistema. O writer substitui cada um por `U+FFFD`
e registra `ILLEGAL_CHARACTERS_REPLACED` com a lista de células afetadas: a
conversão conclui, o texto chega à planilha e a substituição é auditável. Antes
dessa correção o arquivo inteiro falhava em `INTERNAL_ERROR` na etapa de
geração, depois de toda a extração ter sido feita.

## Decisões

Toda escolha relevante gera um `Decision` com `algorithm` e `reason_codes`:

| Código | Significado |
| --- | --- |
| `same_line` | rótulo e valor na mesma linha |
| `colon_separator` / `equals_separator` | separador explícito |
| `value_below` | valor na linha seguinte |
| `multiline_value` | valor ocupou mais de uma linha |
| `two_columns` + `consistent_alignment` | colunas geométricas |
| `font_style_transition` | negrito do rótulo separando do valor |
| `label_at_line_start` | rótulo inicia a linha |
| `transposed_table` | tabela de rótulos convertida em registro |
| `table_continuation` | tabela unificada entre páginas |

Uma decisão rejeitada também é registrada — inclusive as alternativas de tabela
com pontuação, para que uma conversão ruim possa ser explicada.

## As 12 abas de "Ver Auditoria"

1. **Visão Geral** — qualidade, métricas, motores, validação da cadeia.
2. **Linha do Tempo** — eventos cronológicos com filtros por fase/tipo/gravidade.
3. **Páginas** — classificação, fonte de texto, regiões, blocos, tabelas.
4. **Campos** — rótulo → valor com evidência.
5. **Tabelas** — estratégia, cabeçalhos, mesclagens, continuação, alternativas.
6. **Dados Extraídos** — todos os blocos e o destino de cada um.
7. **Excel Gerado** — explorador de células; clique abre a proveniência.
8. **Decisões** — algoritmo, códigos de razão, alternativas.
9. **Inconsistências** — avisos/erros com link para a página.
10. **Desempenho** — tempos por fase.
11. **Auditoria Técnica** — JSON bruto dos eventos, com hashes.
12. **Exportar** — JSON completo ou relatório TXT.

## Armazenamento em duas camadas

- **Banco**: índice pesquisável (run, página, fase, tipo, gravidade, célula).
- **Storage**: `runs/{run_id}/audit.json.gz` e `runs/{run_id}/output.xlsx`,
  com SHA-256, versão e política de retenção.

O botão consulta o banco; artefatos volumosos só são baixados sob demanda.

## Severidades

| Severidade | Exemplo |
| --- | --- |
| `info` | tabela unificada, locale detectado |
| `warning` | `OCR_UNAVAILABLE`, `IMAGE_REGION_NOT_RECOGNIZED`, `PAGE_WITHOUT_TEXT`, `FIELD_WITHOUT_VALUE` |
| `error` | `PAGE_FAILED`, falha na auditoria do XLSX |
| `critical` | falha interna inesperada |

## Páginas não recuperadas

Uma página que não produziu nenhum texto (imagem sem OCR, scan ilegível, falha
do parser) é registrada de forma explícita:

- `page_results.status = "partial"`, `text_source = "none"`;
- eventos `PAGE_ANALYZED`, `OCR_UNAVAILABLE` / `OCR_*`, `PAGE_WITHOUT_TEXT`;
- checagem `unrecognized_regions` com status `warn`, o que leva o resultado a
  `completed_with_warnings` / `partial`.

Isso impede o falso positivo de "100% de cobertura" quando uma página inteira
ficou de fora: a cobertura mede o texto recuperado, não o que faltou.

Uma página **legitimamente em branco** (sem texto, imagens ou vetores) não gera
issue e não degrada a qualidade — apenas recebe um evento informativo.

## Estado de qualidade

| Estado | Critério |
| --- | --- |
| `validated` | todas as checagens `pass` |
| `partial` | existe ao menos um `warn`/`fail`, mas há XLSX utilizável |
| `failed` | nenhum conteúdo recuperável ou falha fatal |

O estado é derivado das checagens persistidas — não de uma opinião do motor.
As checagens da auditoria do XLSX (arquivo válido, ausência de imagens e de
fórmulas, correspondência célula a célula) entram nesse cálculo **depois** de
executadas: uma divergência encontrada ao reabrir a planilha rebaixa o
resultado. `failed` nunca é apresentado como `completed_with_warnings`.
