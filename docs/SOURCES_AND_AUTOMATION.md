# Fontes por caminho, planilhas e agendamentos

Este documento descreve o comportamento **implementado** (não proposto) das
funcionalidades de upgrade. Ele é a referência operacional para o site local,
o modo full e o aplicativo desktop.

## 1. Fontes autorizadas

- Uma **raiz concedida** (`source_roots`) é a única forma de conceder acesso ao
  disco. No desktop ela é criada pelo seletor nativo com a credencial do host;
  no site local, o operador concede pela rota interna autenticada.
- O navegador sempre trabalha com caminho **relativo** a uma raiz já concedida.
  Digitar `C:\Planilhas` no navegador não concede nada.
- A contenção é por componentes: `C:\Dados2` não está dentro de `C:\Dados`.
  UNC, unidades de rede mapeadas, `..`, ADS, namespaces de dispositivo e nomes
  especiais são recusados. Junctions/reparse points não são seguidos na
  varredura, e a abertura revalida o caminho real (`PATH_ESCAPED`).
- O snapshot copia o conteúdo em streaming para staging privado, com hash
  SHA-256. O arquivo muda durante a cópia? `SOURCE_CHANGED`. Está aberto no
  Excel? `SOURCE_BUSY` (outros arquivos continuam).

## 2. Entrada de planilhas (XLSX/CSV)

- XLSX é reconhecido **pelo conteúdo** (pacote OOXML). ZIP renomeado é
  `NOT_SPREADSHEET`; orçamentos de expansão/entradas/tempo geram
  `SPREADSHEET_BUDGET_EXCEEDED`, nunca truncamento silencioso.
- `.xls`, `.xlsm`, `.xlsb`, `.ods` são recusados com mensagem explícita.
- Cada aba é uma unidade lógica (a UI diz **aba**). Valores originais são
  preservados (zeros à esquerda continuam texto), a evidência é o endereço
  real da célula — nunca um bbox inventado.
- Fórmula sem valor em cache vira **texto** com aviso `FORMULA_WITHOUT_CACHE`;
  nada é recalculado.
- CSV exige encoding e dialeto determinísticos: sem declaração, apenas UTF-8;
  ambiguidade gera `CSV_DIALECT_REQUIRED` e bytes não decodificáveis geram
  `CSV_ENCODING_REQUIRED`. Em agendamentos, a fonte/opções guardam o dialeto.
- Saídas: XLSX normalizado, TXT por aba e CSV por tabela (ZIP quando >1).

## 3. Importação

`POST /api/v1/sources/{id}/imports` (header `Idempotency-Key` obrigatório):

1. verificação de dono, raiz e fonte;
2. varredura em streaming com filtros e exclusões (`~$*`, `*.tmp` por padrão);
3. captura e hash de cada candidato;
4. deduplicação por `(fonte, caminho, sha256, options_hash)` — decidida **após**
   a captura, então mudanças reais nunca são mascaradas;
5. submissão pelo lote durável existente com `client_item_id` estável;
6. acompanhamento até o estado terminal e registro da versão bem-sucedida.

Repetir a mesma chave com o mesmo corpo devolve a mesma importação; corpo
diferente retorna 409 `IDEMPOTENCY_KEY_REUSED`. Duas importações simultâneas da
mesma fonte retornam 409 `SOURCE_IMPORT_BUSY`. Uma queda em qualquer fronteira
retoma pelo `import_id` sem duplicar lote ou conversão.

## 4. Agendamentos

- Diário usa horário local + fuso IANA. Horário inexistente (DST) resolve no
  primeiro instante válido; horário repetido dispara uma única vez.
- Intervalo é ancorado em UTC (`anchor + n × intervalo`): a duração da execução
  nunca desloca o calendário. Limites: 60 s a 365 dias.
- A **prévia** e o disparo real usam a mesma função de calendário.
- Horários perdidos: `run_once_latest` (padrão) executa só a última ocorrência e
  registra quantas foram condensadas; `skip` registra uma ocorrência `skipped`.
- Sobreposição: `skip` ignora a nova ocorrência; `queue_one` mantém no máximo
  uma pendente.
- Pausar impede novas ocorrências (a execução ativa continua); retomar começa do
  próximo horário futuro, sem backlog.
- Cada ocorrência congela a **definição completa da fonte** no snapshot: editar
  a fonte depois não altera arquivos de uma ocorrência antiga. Revogar a raiz
  ainda bloqueia a leitura (`ROOT_REVOKED`).
- Estados: `queued → running → waiting_children → succeeded | partial |
  no_changes | failed`; `skipped` e `cancelled` conforme a política. `succeeded`
  só depois de todos os filhos terminais (e da indexação, se solicitada).
- Cancelar é durável e idempotente; resultado já publicado permanece histórico.

## 5. Indexação (RAG)

- A busca continua **lexical**; trocar o LLM gerador não reconstrói chunks.
- No modo full, o gateway resolve `conversion_id → documento/run` no Prisma com
  ownership e envia referências autorizadas ao motor; um id alheio responde
  `not_found` sem chamada.
- Indexação agendada é durável: jobs persistidos, recuperação de `running`
  órfãos no startup.

## 6. Modelos locais (desktop)

- Modos `external` (não reconfigura nem reinicia o serviço existente) e
  `managed` (processo próprio, `OLLAMA_MODELS` aplicado só a ele).
- Testar é real e separado: OCR com imagem de exemplo, LLM com resposta curta.
  Listar tags não prova compatibilidade.
- Aplicar grava o perfil atomicamente, reinicia só processos gerenciados e
  reporta `applied | deferred | rolled_back`.
- Endpoints precisam ser loopback nesta versão; modelo remoto não é fallback.

## 7. Flags

| Flag | Efeito |
| --- | --- |
| `SOURCE_IMPORTS_ENABLED` | Recusa novas importações; histórico permanece |
| `SPREADSHEET_INPUT_ENABLED` | Recusa admissão de XLSX/CSV |
| `AUTOMATION_EXECUTOR_ENABLED` | Liga o loop de calendário no processo standalone |
| `DESKTOP_HOST_ENABLED` | Habilita a capacidade de host desktop |
| `AUTOMATION_POLL_SECONDS` | Intervalo do loop (padrão 5 s) |
| `AUTOMATION_HISTORY_RETENTION_DAYS` | Histórico detalhado (padrão 90) |
| `AUTOMATION_FAILURE_RETENTION_DAYS` | Falhas (padrão 7) |

A retenção nunca apaga originais nem artefatos de conversão.
