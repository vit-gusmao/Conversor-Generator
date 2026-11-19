# Empacotamento do backend (T11)

## Build completo (recomendado)

Na raiz do repositório, com `services/converter/.venv` criada e com
`pyinstaller` instalado nela:

```powershell
npm run build:desktop
```

`scripts/build-desktop.ps1` executa, em ordem e parando na primeira falha:

1. `packages/contracts` (`npm ci` se faltar `node_modules`, depois `build`);
2. `apps/web` (idem; gera `apps/web/dist`);
3. PyInstaller com a venv do serviço (`converter.spec`, onedir), com checagens
   de `python3.dll`, `VCRUNTIME140.dll` e `python3t.dll` (aviso se ausente);
4. smoke test do binário congelado (`converter.exe --no-serve`, em diretório
   temporário fora do repositório, com `APP_DATA_DIR` isolado): exige
   `CONVERTER_READY` e o banco criado;
5. `apps/desktop` (`npm ci` se faltar, `npm run build`);
6. `electron-builder --win nsis --x64` (também gera `win-unpacked`).

Saídas em `apps/desktop/release/`:

- `win-unpacked/conversor-generator.exe`
- `win-unpacked/resources/backend/converter.exe` (+ `_internal/`)
- `win-unpacked/resources/web/index.html`
- `conversor-generator-setup-<versão>.exe`

`-OutputDirectory <nome>` escolhe outra pasta dentro de `apps/desktop` (usado
quando `release/` está bloqueada por outro processo).

## Build só do backend

```powershell
# na raiz do repositório, com a venv do serviço
services\converter\.venv\Scripts\python -m PyInstaller --clean --noconfirm packaging\converter.spec
```

Rode a partir de `services/converter`. Saída: `dist/converter/` (modo
diretório) com `converter.exe`. O `apps/web/dist` precisa existir antes; o spec
recusa o build sem ele e recusa bundle se houver `.env*` ou banco dentro de
`apps/web/dist`.

## O que entra no pacote

- o código Python do serviço (módulos importados pelo `desktop_entry.py`);
- `apps/web/dist` como recurso **somente leitura** (`web/`);
- `cryptography` (binários Rust) e a DLL `python3t.dll` do shim `abi3t`;
  o spec procura essa DLL em `<base_prefix>\abi3t-compat`, em `<base_prefix>` e
  no diretório do `python.exe` de build, nesta ordem;
- o runtime hook `rthook_dll.py`, que registra os diretórios de DLL.

Nada do repositório fora de `apps/web/dist` é coletado como dado: `.env`,
bancos, `uploads/`, `storage/`, `work/` e `.venv/` ficam fora do pacote. Todo
estado mutável vive em `APP_DATA_DIR` (userData do desktop).

## Contrato de readiness

`desktop_entry.py` publica `CONVERTER_READY {...}` (stdout e, se houver,
`--readiness-file`) somente depois de:

1. criar e aplicar os diretórios de runtime e rodar `init_db`;
2. importar `api.main` (o app ASGI) com sucesso;
3. ligar o socket em loopback na porta que o SO atribuiu.

Uma falha de importação (por exemplo, DLL ausente) encerra o processo com erro
e nunca anuncia prontidão. O supervisor Electron (`apps/desktop`) lê a porta
real dessa linha; uma saída inesperada depois de pronto é tratada pelo
aplicativo (um reinício automático e depois um diálogo para reiniciar ou sair).

## Observação sobre o Python de build

Python 3.15 usa o shim `abi3t`. Wheels nesse ABI (por exemplo `cryptography`
50) importam `python3t.dll`. Se o build sair com o aviso "python3t.dll
ausente", confira `C:\Python315\abi3t-compat` ou reinstale o Python.
