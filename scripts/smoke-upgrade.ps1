# Smoke test do upgrade (T14) - sobe o backend standalone real, exercita o fluxo
# completo e derruba tudo. Nao toca no banco de desenvolvimento: usa um
# APP_DATA_DIR temporario.
#
# Uso (na raiz do repositorio):
#   pwsh scripts/smoke-upgrade.ps1

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
$data = Join-Path ([System.IO.Path]::GetTempPath()) ("cg-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $data | Out-Null
$db = Join-Path $data 'converter.db'

$env:APP_DATA_DIR = $data
$env:WORK_DIR = Join-Path $data 'work'
$env:LOG_DIR = Join-Path $data 'logs'
$env:STORAGE_LOCAL_ROOT = Join-Path $data 'storage'
$env:WEB_DIST_DIR = Join-Path $root 'apps/web/dist'
$env:CONVERTER_DATABASE_URL = "sqlite:///$($db -replace '\\', '/')"
$env:CONVERTER_OCR_ENABLED = 'false'
$env:CONVERTER_QUEUE = 'inline'
$env:AUTOMATION_EXECUTOR_ENABLED = 'true'
$env:AUTOMATION_POLL_SECONDS = '1'
$env:CONVERTER_INTERNAL_TOKEN = 'smoke-internal-token'
$env:AUTOMATION_HISTORY_RETENTION_DAYS = '90'
$env:AUTOMATION_FAILURE_RETENTION_DAYS = '7'

$python = Join-Path $root 'services/converter/.venv/Scripts/python.exe'
if (-not (Test-Path $python)) { throw "venv do conversor nao encontrada: $python" }

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$log = Join-Path $data 'smoke.log'
$process = Start-Process -FilePath $python -ArgumentList '-m', 'desktop_entry', '--port', '0', '--readiness-file', (Join-Path $data 'ready.json') `
  -WorkingDirectory (Join-Path $root 'services/converter') -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden

try {
  # 1) readiness
  $ready = $null
  for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Path (Join-Path $data 'ready.json')) {
      $ready = Get-Content (Join-Path $data 'ready.json') -Raw | ConvertFrom-Json
      break
    }
  }
  if (-not $ready) { throw "backend nao publicou readiness. Log: $((Get-Content $log -Raw))" }
  $base = "http://127.0.0.1:$($ready.port)"
  Write-Host "readiness ok: porta $($ready.port), versao $($ready.version), startup $($stopwatch.ElapsedMilliseconds) ms"

  # 2) health + SPA na mesma origem
  $health = Invoke-RestMethod "$base/health"
  if ($health.status -ne 'ok') { throw "health inesperado: $($health.status)" }
  $spa = Invoke-WebRequest "$base/" -UseBasicParsing
  if ($spa.StatusCode -ne 200) { throw "SPA nao servida na raiz" }

  # 3) capabilities mostram as flags efetivas
  $caps = Invoke-RestMethod "$base/api/v1/capabilities"
  if (-not $caps.features.automation_executor) { throw "flag do executor nao publicada" }
  $inputs = ($caps.inputs | Where-Object { $_.type -eq 'spreadsheet' })
  if (-not $inputs) { throw "planilha ausente em capabilities.inputs" }

  # 4) sessao de dispositivo
  $session = Invoke-RestMethod "$base/api/v1/auth/anonymous" -Method Post -Body '{}' -ContentType 'application/json'
  $auth = @{ Authorization = "Bearer $($session.access_token)" }
  $me = Invoke-RestMethod "$base/api/v1/auth/me" -Headers $auth

  # 5) raiz + fonte + conversao de planilha
  $rootGranted = Join-Path $data 'granted'
  $docs = Join-Path $rootGranted 'planilhas'
  New-Item -ItemType Directory -Path $docs -Force | Out-Null
  $xlsx = Join-Path $docs 'planilha.xlsx'
  $pythonInline = "import sys; from openpyxl import Workbook; wb=Workbook(); ws=wb.active; ws.title='Faturas'; ws.append(['Doc','Valor']); ws.append(['00123456789', 1234.56]); wb.save(sys.argv[1])"
  & $python -c $pythonInline $xlsx
  if (-not (Test-Path $xlsx)) { throw "fixture XLSX nao criada" }

  $internal = @{ 'x-internal-token' = 'smoke-internal-token' }
  $grant = Invoke-RestMethod "$base/internal/v1/source-roots" -Method Post -Headers $internal -ContentType 'application/json' `
    -Body (@{ owner_id = $me.id; canonical_path = $rootGranted } | ConvertTo-Json)
  $source = Invoke-RestMethod "$base/api/v1/sources" -Method Post -Headers $auth -ContentType 'application/json' `
    -Body (@{ root_id = $grant.id; relative_path = 'planilhas'; kind = 'directory'; name = 'Smoke'; include_patterns = @('*.xlsx') } | ConvertTo-Json)
  try {
    $validation = Invoke-RestMethod "$base/api/v1/sources/$($source.id)/validate" -Method Post -Headers $auth
  } catch {
    $body = $_.ErrorDetails.Message
    throw "validacao falhou: $body"
  }
  if ($validation.estimated_files -ne 1) { throw "validacao nao encontrou o arquivo" }

  $importKey = [guid]::NewGuid().ToString('N')
  $import = Invoke-RestMethod "$base/api/v1/sources/$($source.id)/imports" -Method Post -Headers ($auth + @{ 'Idempotency-Key' = $importKey }) `
    -ContentType 'application/json' -Body (@{ options = @{ schema_version = 3 } } | ConvertTo-Json -Depth 5)

  $final = $null
  for ($i = 0; $i -lt 240; $i++) {
    Start-Sleep -Milliseconds 500
    $final = Invoke-RestMethod "$base/api/v1/imports/$($import.id)" -Headers $auth
    if ($final.status -in @('completed','partial','failed','cancelled')) { break }
  }
  if ($final.status -ne 'completed') { throw "importacao terminou em $($final.status): $($final.error | ConvertTo-Json)" }
  if ($final.counts.imported -ne 1) { throw "importacao nao converteu o arquivo" }
  Write-Host "importacao ok: $($final.counts | ConvertTo-Json -Compress)"

  # 6) agenda + run-now pelo executor embutido
  $schedule = Invoke-RestMethod "$base/api/v1/schedules" -Method Post -Headers $auth -ContentType 'application/json' -Body (@{
    name = 'Smoke'; source_id = $source.id; rule = @{ kind = 'interval'; every_seconds = 3600; anchor_at = $null };
    timezone = 'UTC'; options = @{ schema_version = 3 }
  } | ConvertTo-Json -Depth 5)
  $run = Invoke-RestMethod "$base/api/v1/schedules/$($schedule.id)/run-now" -Method Post `
    -Headers ($auth + @{ 'Idempotency-Key' = [guid]::NewGuid().ToString('N') }) -ContentType 'application/json' -Body '{}'
  $runDetail = $null
  for ($i = 0; $i -lt 240; $i++) {
    Start-Sleep -Milliseconds 500
    $runDetail = Invoke-RestMethod "$base/api/v1/schedule-runs/$($run.run_id)" -Headers $auth
    if ($runDetail.state -in @('succeeded','partial','no_changes','failed','cancelled','skipped')) { break }
  }
  if ($runDetail.state -notin @('succeeded','no_changes')) { throw "ocorrencia terminou em $($runDetail.state)" }
  Write-Host "agendamento ok: estado $($runDetail.state)"

  Write-Host "SMOKE OK - dados em $data"
}
finally {
  if ($process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
  }
}
