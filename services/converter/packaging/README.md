# Empacotamento do backend (T11)

```powershell
# na raiz do repositório, com a venv do serviço ativa
pyinstaller --clean --noconfirm services/converter/packaging/converter.spec
```

Saída: `dist/converter/` (modo diretório) com `converter.exe`. O `apps/web/dist`
é copiado para dentro do pacote como recurso **somente leitura**; banco, storage,
work, logs, backups e identidade vivem em `APP_DATA_DIR` (userData do desktop).

O supervisor Electron (`apps/desktop`) é quem fornece esses diretórios absolutos
antes do processo importar `config.settings`, e lê a porta real pelo arquivo de
readiness (`CONVERTER_READY {...}` no stdout ou `--readiness-file`).
