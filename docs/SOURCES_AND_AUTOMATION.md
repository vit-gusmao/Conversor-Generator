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
- A raiz é gravada pelo caminho real (`realpath`) no momento da concessão, e a
  revalidação compara caminhos reais dos dois lados. Assim, uma raiz que é
  junction, `subst` ou nome 8.3 não parece uma fuga.
- Nomes reservados do Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
  `LPT1`–`LPT9`, incluindo os dígitos sobrescritos) são comparados pelo radical
  exato, antes do primeiro ponto, sem diferenciar maiúsculas. `Compras.xlsx` e
  `Comissões` são nomes comuns; `con.txt` e `COM1` são recusados com
  `SPECIAL_PATH_DENIED`. Espaços finais no radical são ignorados, como no Windows.
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
2. varredura em streaming com filtros e exclusões (padrão: `~$*`, `*.tmp`,
   `~wrd*`);
3. captura e hash de cada candidato;
4. deduplicação por `(fonte, caminho, sha256, options_hash)` — decidida **após**
   a captura, então mudanças reais nunca são mascaradas;
5. submissão pelo lote durável existente com `client_item_id` estável;
6. acompanhamento até o estado terminal e registro da versão bem-sucedida.

Repetir a mesma chave com o mesmo corpo devolve a mesma importação; corpo
diferente retorna 409 `IDEMPOTENCY_KEY_REUSED`. Duas importações simultâneas da
mesma fonte retornam 409 `SOURCE_IMPORT_BUSY`. Com `SOURCE_IMPORTS_ENABLED=false`,
o início responde 409 `FEATURE_DISABLED`; o histórico continua legível.

Estados da importação: `pending → scanning → submitting → converting → indexing`
e, no fim, `completed | partial | failed | cancelled`. Estados dos itens:
`captured`, `submitted`, `queued`, `completed`, `skipped`, `failed`. Em item
com falha, o código fica no próprio item (`SOURCE_CHANGED`, `SOURCE_BUSY`,
`SOURCE_MISSING`, `FILE_TOO_LARGE`, `SNAPSHOT_MISSING`, `CONVERSION_FAILED`).

**Retomada.** Uma importação com `status` `partial` ou `failed` e erro
`IMPORT_INCOMPLETE` (itens ainda em andamento) ou `IMPORT_GATEWAY_UNAVAILABLE`
(falha transitória ao submeter ao gateway) pode ser executada de novo. A nova
execução reconcilia os itens pelas mesmas chaves determinísticas, sem criar
lote ou conversão duplicados. `completed` e `cancelled` não são retomadas.
Um arquivo que sumiu da fonte marca `source_missing` nas versões já importadas;
a varredura completa que não o encontra marca a origem como ausente.

**Concorrência.** O lease da importação dura 120 s, renovado a cada atualização
de progresso. Uma importação `pending` conta como ativa por uma janela de 120 s
após a criação, para não se sobrepor nem bloquear a fonte para sempre.

Uma queda em qualquer fronteira retoma pelo `import_id` sem duplicar lote ou
conversão.

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
  no_changes | failed`; `cancelling` entre o pedido e o fim; `skipped` e
  `cancelled` conforme a política. `succeeded` só depois de todos os filhos
  terminais (e da indexação, se solicitada).
- Falhas transitórias (gateway indisponível ou com timeout, HTTP 5xx, 408, 429,
  erro de conexão) devolvem a ocorrência para `queued` com horário mínimo de
  nova tentativa: 30 s, 120 s e 300 s após a 1ª, 2ª e 3ª falha. A tentativa
  seguinte retoma a mesma importação. Qualquer outra falha, ou a tentativa
  depois do último intervalo, é terminal.
- O lease da ocorrência dura 120 s e é renovado a cada 30 s (heartbeat). Um
  lease vencido é recuperado no início do loop ou na próxima varredura, com
  evento `LEASE_RECOVERED`.
- **Run-now** (`POST /schedules/{id}/run-now`): exige `Idempotency-Key`. A mesma
  chave devolve a mesma ocorrência manual. Não desloca o calendário. Se a
  política de sobreposição for `skip` e já houver ocorrência ativa, a nova
  nasce `skipped`. A resposta é 202 com `executor_active`:
  - `true`: este processo executa a ocorrência agora;
  - `false`: a ocorrência fica `queued` para um executor externo
    (`python -m automation.cli` no modo full, ou o aplicativo desktop).
  Não há mais `FEATURE_DISABLED` nesta rota.
- Cancelar é durável e idempotente; resultado já publicado permanece histórico.

## 5. Indexação (RAG)

- A busca continua **lexical**; trocar o LLM gerador não reconstrói chunks.
- No modo full, o gateway resolve `conversion_id → documento/run` no Prisma com
  ownership e envia referências autorizadas ao motor; um id alheio responde
  `not_found` sem chamada.
- Indexação agendada é durável: jobs persistidos em `knowledge_index_jobs`,
  com lease e renovação. O loop de automação despacha os jobs deixados em
  `queued` e os leases de workers mortos. No startup, os jobs `running` órfãos
  são recuperados.
- A busca consulta somente as versões ativas dos documentos: cada fonte tem a
  cabeça (`source_knowledge_heads`) apontando para a geração vigente.

## 6. Modelos locais (desktop)

- Modos `external` (não reconfigura nem reinicia o serviço existente) e
  `managed` (processo próprio, `OLLAMA_MODELS` aplicado só a ele).
- Testar é real e separado: OCR com imagem de exemplo, LLM com resposta curta.
  Listar tags não prova compatibilidade.
- Aplicar grava o perfil atomicamente, reinicia só processos gerenciados e
  reporta `applied | deferred | rolled_back`.
- Endpoints precisam ser loopback nesta versão; modelo remoto não é fallback.

## 7. Flags

| Flag | Padrão no código | Efeito |
| --- | --- | --- |
| `SOURCE_IMPORTS_ENABLED` | `true` | Recusa novas importações (`409 FEATURE_DISABLED`); histórico permanece |
| `SPREADSHEET_INPUT_ENABLED` | `true` | Recusa admissão de XLSX/CSV |
| `AUTOMATION_EXECUTOR_ENABLED` | `false` | Liga o loop de calendário no processo standalone |
| `DESKTOP_HOST_ENABLED` | `false` | Habilita a capacidade de host desktop |
| `AUTOMATION_POLL_SECONDS` | `5` | Intervalo do loop |
| `AUTOMATION_HISTORY_RETENTION_DAYS` | `90` | Histórico detalhado |
| `AUTOMATION_FAILURE_RETENTION_DAYS` | `7` | Ocorrências com falha |

Quem liga o executor, na prática:

- Código (standalone): `false` se a variável não estiver definida.
- `starter-local-server.cmd`: define `AUTOMATION_EXECUTOR_ENABLED=true` quando a
  variável não está definida.
- Aplicativo desktop: liga a flag no perfil gerenciado do processo do motor.
- Modo full (`infra/docker-compose.yml`): o serviço de automação roda
  `python -m automation.cli` com a flag `true`. O processo da API não executa
  ocorrências nesse modo.

A retenção nunca apaga originais nem artefatos de conversão.
