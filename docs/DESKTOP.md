# Aplicativo Windows (Electron) — operação

## 1. O que ele é

Um invólucro Electron que **supervisiona** o backend Python empacotado e abre a
mesma SPA React servida por ele em loopback. Não há cópia de telas nem pipeline
no Electron.

```text
conversor-generator.exe (Electron)
  ├─ perfil JSON privado (userData/profile.json)
  ├─ backend congelado (resources/backend/converter.exe)
  │    └─ SQLite + storage + work + logs em userData
  ├─ servidor loopback com porta escolhida pelo SO
  └─ janela sandbox → SPA da mesma origem (/api/v1 relativo)
```

## 2. Instalação e primeira execução

1. Execute `conversor-generator-setup-<versao>.exe` (instalação por usuário).
2. O executável instalado chama-se exatamente `conversor-generator.exe`.
3. Na primeira execução o app cria, em `%APPDATA%\Conversor Generator`:
   `profile.json`, `identity/`, `converter.db`, `storage/`, `work/`, `logs/`,
   `backups/`.
4. Nada mutável é gravado em `Program Files`, nos `resources` ou dentro do ASAR.

## 3. Inicialização (ordem real)

Instância única → leitura do perfil → pastas e credenciais → spawn do backend
sem shell → `init_db`/migração → bind em porta livre de loopback → publicação de
readiness autenticado (`port`, `boot_id`, `version`) → validação → janela.

Timeout de 90 s mostra diagnóstico com o trecho de stderr e permite tentar de
novo. Modelo ausente **não** impede abrir a interface.

## 4. Configurações → IA local

- Modo externo (Ollama já ativo) ou gerenciado (inicia o próprio).
- Executável, pasta de modelos, tag de OCR e tag do LLM são campos separados.
- “Testar modelos” faz uma chamada real de visão e outra de geração.
- “Aplicar perfil” grava atomicamente e reinicia só os processos gerenciados;
  `deferred`/`rolled_back` aparecem na tela com o motivo.
- GGUF: a importação usa um Modelfile **gerado pelo app** e argumentos fixos.
  Nenhum peso é baixado automaticamente.

## 5. Bandeja e encerramento

- Fechar a janela mantém na bandeja quando a preferência está ativa (padrão).
- Menu da bandeja: abrir, pausar novos agendamentos, sair.
- “Sair” bloqueia novos disparos, aguarda encerramento gracioso e termina
  **somente** a árvore de processos criada pelo app. Queda forçada é recuperada
  por leases/checkpoints.
- O app não encerra um Ollama externo.

## 6. Segurança

- `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`, preload
  mínimo e sem `fs`/`exec`/`spawn` genérico.
- Navegação para fora da origem do backend é bloqueada; `window.open` é negado.
- Todo canal IPC valida a origem do frame e os argumentos. Cancelar um diálogo
  retorna `null` e não apaga uma configuração válida.
- A credencial de bootstrap do host nunca é exposta ao renderer.

## 7. Atualização

A primeira estratégia é o instalador manual: ele preserva `userData`. O backend
migra o schema com lock e backup consistente em `backups/`; falha bloqueia a
abertura com diagnóstico. Rollback de binário só é permitido com schema
compatível ou restauração coordenada do backup. Auto-update fica para uma etapa
separada (validando assinatura/origem).

## 8. Limites conhecidos desta entrega

- Sem execução com Windows deslogado ou app totalmente fechado (a bandeja
  mantém o processo; serviço Windows é uma extensão separada).
- Sem conector para importar pastas de outro PC em um site remoto.
- Sem garantia de GPU/VRAM: latências dependem do hardware medido.
- Instalação e empacotamento reais precisam de máquina Windows limpa (ver
  `.github/workflows/desktop.yml`); os testes desta entrega cobrem o supervisor,
  o perfil, o IPC e o entry point congelável com doubles.
