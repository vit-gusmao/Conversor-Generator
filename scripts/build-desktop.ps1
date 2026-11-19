# Pipeline de distribuição do aplicativo Windows (Conversor Generator).
#
# Ordem (cada etapa só roda se a anterior passou):
#   1. packages/contracts  -> npm ci (se faltar node_modules) + build
#   2. apps/web            -> npm ci (se faltar node_modules) + build (SPA)
#   3. services/converter  -> PyInstaller com a venv .venv (onedir) + checagens
#   4. smoke test do backend congelado (--no-serve, fora do repositório)
#   5. apps/desktop        -> npm ci (se faltar node_modules) + build (tsc)
#   6. electron-builder    -> NSIS x64 + win-unpacked, em apps/desktop/release
#
# Uso (na raiz do repositório):
#   npm run build:desktop
#   powershell -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
#
# -OutputDirectory: nome da pasta de saída dentro de apps/desktop (padrão
# 'release'). Use outro nome só se a pasta padrão estiver bloqueada por outro
# processo; o nome precisa ser simples, sem separadores de caminho.
#
# Compatível com Windows PowerShell 5.1. Caminhos com espaços são seguros:
# todo caminho é montado com Path::Combine e passado como argumento de array.
#
# Observação sobre assinatura: CSC_IDENTITY_AUTO_DISCOVERY=false impede o
# electron-builder de procurar certificado (não há certificado de código aqui).
# O rcedit continua ativo, então o .exe leva metadados e ícone do produto.

param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$OutputDirectory = 'release'
)

$ErrorActionPreference = 'Stop'

$Root       = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PSScriptRoot, '..'))
$Contracts  = [System.IO.Path]::Combine($Root, 'packages', 'contracts')
$Web        = [System.IO.Path]::Combine($Root, 'apps', 'web')
$Converter  = [System.IO.Path]::Combine($Root, 'services', 'converter')
$Python     = [System.IO.Path]::Combine($Converter, '.venv', 'Scripts', 'python.exe')
$BackendDir = [System.IO.Path]::Combine($Converter, 'dist', 'converter')
$Desktop    = [System.IO.Path]::Combine($Root, 'apps', 'desktop')
$Release    = [System.IO.Path]::Combine($Desktop, $OutputDirectory)
$Builder    = [System.IO.Path]::Combine($Desktop, 'node_modules', '.bin', 'electron-builder.cmd')

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-Build([string]$Message) {
    Write-Host "ERRO: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Native([string]$What, [string]$Program, [string[]]$Arguments, [string]$WorkingDirectory) {
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Program @Arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        Stop-Build "$What falhou (código de saída $code)."
    }
}

function Require-File([string]$Path, [string]$Hint) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Stop-Build "arquivo ausente: $Path. $Hint"
    }
}

function Install-IfMissing([string]$Directory, [string]$Label) {
    if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($Directory, 'node_modules')))) {
        Write-Host "node_modules ausente em ${Label}: executando npm ci."
        Invoke-Native "npm ci ($Label)" 'npm.cmd' @('ci') $Directory
    }
}

# --- pré-requisitos ------------------------------------------------------------
Write-Step 'Verificando pré-requisitos'
foreach ($tool in @('node', 'npm.cmd')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Stop-Build "'$tool' não encontrado no PATH. Instale o Node.js (versão de .nvmrc)."
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Stop-Build ("venv do backend ausente em $Python. Crie com: " +
        "python -m venv services\converter\.venv ; " +
        "services\converter\.venv\Scripts\python -m pip install -e `".[test,migrate]`" pyinstaller")
}
Invoke-Native 'verificação do PyInstaller' $Python @('-c', 'import PyInstaller') $Converter
Require-File $Builder 'Rode npm ci em apps/desktop (electron-builder ausente).'

# --- 1. contratos ---------------------------------------------------------------
Write-Step 'Contratos (packages/contracts)'
Install-IfMissing $Contracts 'packages/contracts'
Invoke-Native 'build de packages/contracts' 'npm.cmd' @('run', 'build') $Contracts

# --- 2. SPA ---------------------------------------------------------------------
Write-Step 'SPA (apps/web)'
Install-IfMissing $Web 'apps/web'
Invoke-Native 'build de apps/web' 'npm.cmd' @('run', 'build') $Web
Require-File ([System.IO.Path]::Combine($Web, 'dist', 'index.html')) 'O build da SPA não gerou index.html.'

# --- 3. backend congelado -------------------------------------------------------
Write-Step 'Backend congelado (PyInstaller, onedir)'
if (Test-Path -LiteralPath $BackendDir) {
    # Never reuse an earlier bundle: a stale _internal caused a real DLL failure.
    Remove-Item -LiteralPath $BackendDir -Recurse -Force
}
Invoke-Native 'PyInstaller' $Python @('-m', 'PyInstaller', '--clean', '--noconfirm', 'packaging\converter.spec') $Converter

$BackendExe = [System.IO.Path]::Combine($BackendDir, 'converter.exe')
$Internal   = [System.IO.Path]::Combine($BackendDir, '_internal')
Require-File $BackendExe 'O PyInstaller não gerou converter.exe.'
Require-File ([System.IO.Path]::Combine($Internal, 'python3.dll')) 'python3.dll ausente no pacote.'
Require-File ([System.IO.Path]::Combine($Internal, 'VCRUNTIME140.dll')) 'VCRUNTIME140.dll ausente no pacote.'
if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($Internal, 'python3t.dll')))) {
    Write-Host 'AVISO: python3t.dll ausente em _internal. Se o cryptography exigir o shim abi3t,' -ForegroundColor Yellow
    Write-Host '       o smoke test abaixo falhará. Verifique <base_prefix>\abi3t-compat no Python de build.' -ForegroundColor Yellow
}

# --- 4. smoke test do backend congelado ----------------------------------------
Write-Step 'Smoke test do backend congelado (--no-serve, diretório fora do repositório)'
$SmokeRoot = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), 'cg-smoke-' + [guid]::NewGuid().ToString('N'))
$SmokeData = [System.IO.Path]::Combine($SmokeRoot, 'userdata')
New-Item -ItemType Directory -Force -Path $SmokeData | Out-Null
$previousEnv = @{}
# O backend recusa iniciar sem os três segredos do supervisor (desktop_entry.py).
$smokeSecretBuffer = New-Object byte[] 32
$smokeRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $smokeRng.GetBytes($smokeSecretBuffer) } finally { $smokeRng.Dispose() }
$smokeSecret = (($smokeSecretBuffer | ForEach-Object { $_.ToString('x2') }) -join '')
$smokeEnv = @{
    APP_DATA_DIR          = $SmokeData
    WORK_DIR              = [System.IO.Path]::Combine($SmokeData, 'work')
    LOG_DIR               = [System.IO.Path]::Combine($SmokeData, 'logs')
    STORAGE_LOCAL_ROOT    = [System.IO.Path]::Combine($SmokeData, 'storage')
    WEB_DIST_DIR          = [System.IO.Path]::Combine($Web, 'dist')
    CONVERTER_OCR_ENABLED = 'false'
    CONVERTER_QUEUE       = 'inline'
    CONVERTER_INTERNAL_TOKEN = $smokeSecret
    DESKTOP_BOOTSTRAP_SECRET = $smokeSecret
    JWT_SECRET            = $smokeSecret
}
foreach ($key in $smokeEnv.Keys) {
    $previousEnv[$key] = [System.Environment]::GetEnvironmentVariable($key)
    [System.Environment]::SetEnvironmentVariable($key, $smokeEnv[$key])
}
try {
    Push-Location -LiteralPath $SmokeRoot
    try {
        $output = & $BackendExe '--no-serve'
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { Stop-Build "smoke test do backend falhou (código $code)." }
    if (-not ($output -match 'CONVERTER_READY')) { Stop-Build 'smoke test do backend: CONVERTER_READY ausente na saída.' }
    if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($SmokeData, 'converter.db')))) {
        Stop-Build 'smoke test do backend: o banco não foi criado em APP_DATA_DIR.'
    }
    Write-Host ($output -match 'CONVERTER_READY' | Select-Object -First 1)
} finally {
    foreach ($key in $smokeEnv.Keys) {
        [System.Environment]::SetEnvironmentVariable($key, $previousEnv[$key])
    }
    Remove-Item -LiteralPath $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 5. shell Electron ----------------------------------------------------------
Write-Step 'Shell Electron (apps/desktop)'
Install-IfMissing $Desktop 'apps/desktop'
Invoke-Native 'build de apps/desktop' 'npm.cmd' @('run', 'build') $Desktop

# --- 6. empacotamento -----------------------------------------------------------
Write-Step 'Empacotamento (electron-builder: NSIS x64 + win-unpacked)'
if (Test-Path -LiteralPath $Release) {
    # Output from an earlier run must not be mistaken for this build's result.
    try {
        Remove-Item -LiteralPath $Release -Recurse -Force -ErrorAction Stop
    } catch {
        Stop-Build ("não foi possível limpar ${Release}: " + $_.Exception.Message +
            '. Feche o processo que mantém o arquivo aberto ou use -OutputDirectory <outra-pasta>.')
    }
}
$previousCsc = [System.Environment]::GetEnvironmentVariable('CSC_IDENTITY_AUTO_DISCOVERY')
[System.Environment]::SetEnvironmentVariable('CSC_IDENTITY_AUTO_DISCOVERY', 'false')
try {
    Invoke-Native 'electron-builder' $Builder @('--win', 'nsis', '--x64', '--publish', 'never', "--config.directories.output=$OutputDirectory") $Desktop
} finally {
    [System.Environment]::SetEnvironmentVariable('CSC_IDENTITY_AUTO_DISCOVERY', $previousCsc)
}

# --- verificação do resultado ---------------------------------------------------
Write-Step 'Verificando artefatos'
$Unpacked  = [System.IO.Path]::Combine($Release, 'win-unpacked')
$AppExe    = [System.IO.Path]::Combine($Unpacked, 'conversor-generator.exe')
$Installer = Get-ChildItem -LiteralPath $Release -Filter 'conversor-generator-setup-*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
Require-File $AppExe 'electron-builder não gerou conversor-generator.exe.'
Require-File ([System.IO.Path]::Combine($Unpacked, 'resources', 'backend', 'converter.exe')) 'backend ausente em resources/backend.'
Require-File ([System.IO.Path]::Combine($Unpacked, 'resources', 'backend', '_internal', 'python3.dll')) 'python3.dll ausente em resources/backend/_internal.'
Require-File ([System.IO.Path]::Combine($Unpacked, 'resources', 'web', 'index.html')) 'SPA ausente em resources/web.'
if (-not $Installer) { Stop-Build "instalador conversor-generator-setup-*.exe ausente em $Release." }

Write-Host ''
Write-Host 'Build concluído.' -ForegroundColor Green
Write-Host ('  App portátil : ' + $AppExe + ' (' + [math]::Round((Get-Item -LiteralPath $AppExe).Length / 1MB, 1) + ' MB)')
Write-Host ('  Instalador   : ' + $Installer.FullName + ' (' + [math]::Round($Installer.Length / 1MB, 1) + ' MB)')
Write-Host ('  Backend      : ' + $BackendExe)
exit 0
