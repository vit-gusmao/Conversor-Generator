# Banco de dados

## Fronteira

```
pdf2x_api  (NestJS / Prisma)      pdf2x_conv  (Python / SQLAlchemy)
─────────────────────────────     ──────────────────────────────────────
users                             runs
documents                         page_results
conversions                       regions
conversion_runs                   text_elements
conversion_events                 extraction_candidates (via tabelas)
idempotency_keys                  decisions
outbox_events                     extracted_fields
                                  extracted_tables
                                  output_sheets
                                  output_cells
                                  cell_evidence
                                  audit_events
                                  audit_issues
                                  validation_results
                                  coverage_rows
                                  engine_usage
                                  stage_timings
                                  artifacts
public_users*                     
public_documents*                 
public_conversions*               
public_events*                    
idempotency_keys*                 
```

`*` usadas apenas em `RUNTIME_MODE=standalone`, quando o FastAPI também serve a
API pública. Em `full` elas não são escritas.

Em desenvolvimento o padrão é SQLite (`data/api.db`, `data/converter.db`); em
produção, dois bancos PostgreSQL no mesmo cluster.

## Por que não um banco compartilhado

Dois ORMs na mesma tabela significam duas fontes de verdade sobre o schema. O
gateway conhece aplicação (quem pediu, quando, qual arquivo); o motor conhece
evidência (o que foi lido, decidido e escrito). Separar elimina migrações
conflitantes e permite escalar o motor de forma independente.

## Tabelas principais do motor

| Tabela | Conteúdo | Chave |
| --- | --- | --- |
| `runs` | execução, status, progresso, stats, validação | `id` (uuid) |
| `page_results` | página, classificação, chars nativos/OCR, métricas | `run_id`+`page` |
| `regions` | regiões e seus tipos | `run_id`+`region_id` |
| `text_elements` | blocos com bbox, fonte, origem e **destino** | `run_id`+`element_id` |
| `extracted_fields` | rótulo, valor, confiança, separador | `run_id`+`record_id` |
| `extracted_tables` | estratégia, cabeçalhos, linhas, mesclagens, alternativas | `run_id`+`table_id` |
| `output_sheets` | abas, tipo, ordem, páginas de origem | `run_id`+`sheet_id` |
| `output_cells` | célula, valor, original, tipo, formato, origem | único `run,sheet,cell` |
| `cell_evidence` | célula → elemento fonte | `run_id` |
| `audit_events` | evento encadeado por hash | único `run,sequence` |
| `audit_issues` | avisos/erros | `run_id` |
| `validation_results` | checagens e medições | `run_id` |
| `coverage_rows` | cobertura por página | único `run,page` |
| `engine_usage` | motores e contagens | único `run,engine,kind` |
| `stage_timings` | duração por fase | `run_id` |
| `artifacts` | XLSX e manifesto no storage | `run_id`+`kind` |

## Índices

- `output_cells (run_id, sheet)` — explorador de células.
- `audit_events (run_id, sequence)`, `(run_id, stage, sequence)`,
  `(run_id, event_type)`, `(run_id, page_number)` — filtros da auditoria.
- `text_elements (run_id, page_number)`, `(run_id, element_id)`.
- `page_results (run_id, page_number)` único.

## Migrações

| Serviço | Ferramenta | Comando |
| --- | --- | --- |
| Node | Prisma | `npm run db:push`. Em PostgreSQL, o `docker-entrypoint.sh` aplica `prisma db push` a cada inicialização. O repositório não tem pasta de migrações versionadas (`apps/api/prisma/` contém só os schemas e `seed.ts`) |
| Python | create_all + aditivos (`init_db`), e Alembic | `init_db()` roda na inicialização em qualquer modo; `alembic upgrade head` é a alternativa versionada |

A migração inicial do motor está em
`services/converter/migrations/versions/` e foi gerada contra um banco vazio a
partir de `models/db.py`. `init_db()` roda na inicialização do motor em qualquer
modo: cria tabelas ausentes e aplica colunas aditivas. Em ambientes controlados,
prefira `alembic upgrade head`.

## JSON

Payloads variáveis (métricas, contexto de issues, alternativas de tabela) usam
`JSON`. Campos filtrados com frequência são colunas indexadas — nunca ficam
dentro de JSON.
