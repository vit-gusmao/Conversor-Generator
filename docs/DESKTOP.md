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

1. Execute `conversor-generator-setup-<versao>.exe` (instalação por usuário). O instalador ainda não é assinado digitalmente: o Windows SmartScreen pode exibir aviso de editor desconhecido.
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

- A readiness (`CONVERTER_READY {port, boot_id, version}`) só é publicada depois de
  `init_db`/migração e da importação de `api.main`. Uma DLL ausente encerra o
  backend com erro, sem parecer pronto.
- Queda inesperada depois de pronto: o supervisor reinicia o backend **uma vez**
  (o direito é recuperado após uma execução estável). Se falhar de novo, mostra um
  diálogo para reiniciar ou sair.

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
migra o schema com lock e backup consistente em `backups/` (são mantidos os 5
mais recentes, `converter-<UTC>.db`); falha bloqueia a abertura com diagnóstico.
Um banco com schema mais novo que o do build é recusado (`CONVERTER_REFUSED`,
código de saída 78) sem ser alterado. Rollback de binário só é permitido com schema
compatível ou restauração coordenada do backup. Auto-update fica para uma etapa
separada (validando assinatura/origem).

## 8. Limites conhecidos desta entrega

- Sem execução com Windows deslogado ou app totalmente fechado (a bandeja
  mantém o processo; serviço Windows é uma extensão separada).
- Sem conector para importar pastas de outro PC em um site remoto.
- Sem garantia de GPU/VRAM: latências dependem do hardware medido.
- O pacote Windows é gerado e testado pelo workflow `.github/workflows/desktop.yml`
  (`windows-latest`, `workflow_dispatch` e tags `v*`). Não há registro de instalação
  em máquina Windows limpa neste repositório. Os testes unitários do shell
  (`apps/desktop/tests`) rodam no job `desktop` da CI, em Linux, com doubles do Electron.
- Sem assinatura de código (`CSC_IDENTITY_AUTO_DISCOVERY=false`): veja a seção 2.

## 10. Build e verificação do pacote

```powershell
# na raiz do repositório, com services\converter\.venv (com pyinstaller) e npm ci feito
npm run build:desktop
# ou, com outra pasta de saída dentro de apps/desktop (se release/ estiver bloqueada):
powershell -ExecutionPolicy Bypass -File scripts/build-desktop.ps1 -OutputDirectory release-wpa
```

`scripts/build-desktop.ps1` executa, em ordem: contratos, SPA, PyInstaller (onedir,
`packaging/converter.spec`), smoke do backend congelado (`--no-serve`), build do
shell e electron-builder (NSIS x64 + `win-unpacked`). Saídas em
`apps/desktop/release/`: `win-unpacked/conversor-generator.exe`,
`win-unpacked/resources/backend/converter.exe` e
`conversor-generator-setup-<versão>.exe`.

Verificação do resultado:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-desktop.ps1 -AppDir apps/desktop/release/win-unpacked
# -RequireGuard: falha se o backend não tiver o guard de host ativo (usado na CI)
```

O smoke test verifica o layout, o backend sozinho (readiness, `/health`, guard
`403` sem `x-desktop-host` e `200` com ele, banco criado) e o aplicativo por
`-SoakSeconds` (padrão 25) com um perfil temporário, confirmando o `converter.exe`
filho em escuta. A ausência de `python3t.dll` gera aviso, e só falha se o
`cryptography` precisar do shim `abi3t`.

## 9. Segurança do host (WP-E)

- **Segredos por instalação e por início.** `identity/jwt-secret` (96 hex) é criado
  na primeira execução e reutilizado; `identity/device-id` é o dono local. Ambos
  ficam fora do localStorage. O token interno e o segredo do host
  (`DESKTOP_BOOTSTRAP_SECRET`) são novos a cada spawn e nunca vão para a página.
- **Sem valores padrão.** `desktop_entry.py` recusa iniciar (código de saída 78,
  `CONVERTER_REFUSED`) sem os três segredos fornecidos pelo supervisor; após
  importar as configurações, verifica que elas refletem exatamente esses valores.
- **Guard HTTP.** Em modo desktop todo request HTTP exige `x-desktop-host`
  (comparação em tempo constante), `Host` em `127.0.0.1:<porta>` ou
  `localhost:<porta>`, e `Origin` (se presente) igual à própria origem. Única
  exceção: `GET /health`. Não há CORS em modo desktop.
- **Injeção pelo Electron.** `webRequest` adiciona `x-desktop-host` e a CSP
  somente para a origem exata do backend atual; a porta é lida a cada chamada.
- **Drenagem antes de aplicar.** `GET /internal/v1/runtime/activity` conta trabalho
  não terminal. Se houver, a aplicação volta `deferred` sem tocar em perfil ou
  processo; “Aplicar mesmo assim” força. Falha no reinício restaura os bytes do
  perfil anterior e reinicia com ele (`rolled_back`).
- **Ollama gerenciado.** Só o processo iniciado pelo app é encerrado. `OLLAMA_MODELS`
  vai apenas para esse processo. Importação de GGUF: `ollama create <tag> -f <Modelfile>`
  com Modelfile gerado (uma linha `FROM`), sem shell.
