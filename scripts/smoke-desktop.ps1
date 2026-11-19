# Smoke test for the packaged Windows app (electron-builder win-unpacked folder).
#
# Phase 1 (static): conversor-generator.exe, resources\backend\converter.exe,
#   _internal\python3t.dll (warn only), resources\web\index.html.
# Phase 2 (backend alone): runs resources\backend\converter.exe with a scratch
#   profile and random secrets, waits for the CONVERTER_READY line, checks
#   /health, the desktop host guard (403 without x-desktop-host, 200 with it),
#   then kills the process tree.
# Phase 3 (desktop): runs conversor-generator.exe with --user-data-dir pointing
#   at a scratch profile. The app must stay alive for -SoakSeconds, a converter.exe
#   child must listen on a TCP port that answers /health, then the tree is killed.
#
# Usage (any directory):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-desktop.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-desktop.ps1 -AppDir apps/desktop/release-wpa/win-unpacked
#   (CI passes -RequireGuard so a backend without the host guard fails the job.)
#
# -AppDir is resolved against the repository root when relative.
# Exit code: 0 when no check FAILs (warnings allowed), 1 otherwise.
# Compatible with Windows PowerShell 5.1. Keep this file ASCII-only.

param(
    [string]$AppDir = 'apps/desktop/release/win-unpacked',
    [ValidateRange(10, 600)]
    [int]$ReadyTimeoutSeconds = 90,
    [ValidateRange(5, 300)]
    [int]$SoakSeconds = 25,
    [switch]$RequireGuard
)

$ErrorActionPreference = 'Stop'

$Root = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PSScriptRoot, '..'))
if ([System.IO.Path]::IsPathRooted($AppDir)) {
    $App = [System.IO.Path]::GetFullPath($AppDir)
} else {
    $App = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($Root, $AppDir))
}
$GuardSource = [System.IO.Path]::Combine($Root, 'services', 'converter', 'api', 'desktop_guard.py')
$GuardInSource = Test-Path -LiteralPath $GuardSource -PathType Leaf

$AppExe     = [System.IO.Path]::Combine($App, 'conversor-generator.exe')
$BackendDir = [System.IO.Path]::Combine($App, 'resources', 'backend')
$BackendExe = [System.IO.Path]::Combine($BackendDir, 'converter.exe')
$Internal   = [System.IO.Path]::Combine($BackendDir, '_internal')
$WebDir     = [System.IO.Path]::Combine($App, 'resources', 'web')

$Results   = New-Object System.Collections.ArrayList
$Spawned   = New-Object System.Collections.ArrayList
$Scratch   = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), 'cg-smoke-desktop-' + [guid]::NewGuid().ToString('N'))

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host "==> $Title" -ForegroundColor Cyan
}

function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
    $null = $Results.Add([pscustomobject]@{ Check = $Name; Status = $Status; Detail = $Detail })
    $color = 'Gray'
    if ($Status -eq 'PASS') { $color = 'Green' }
    if ($Status -eq 'WARN') { $color = 'Yellow' }
    if ($Status -eq 'FAIL') { $color = 'Red' }
    Write-Host ("[{0}] {1} - {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
}

function New-Secret {
    $buffer = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return (($buffer | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Set-ScopedEnv([hashtable]$Values) {
    $previous = @{}
    foreach ($key in $Values.Keys) {
        $previous[$key] = [System.Environment]::GetEnvironmentVariable($key)
        [System.Environment]::SetEnvironmentVariable($key, [string]$Values[$key])
    }
    return $previous
}

function Restore-Env([hashtable]$Previous) {
    foreach ($key in $Previous.Keys) {
        [System.Environment]::SetEnvironmentVariable($key, $Previous[$key])
    }
}

function Read-SharedText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    $stream = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $reader = New-Object System.IO.StreamReader($stream)
        try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally {
        $stream.Dispose()
    }
}

function Invoke-Get([string]$Url, [hashtable]$Headers = @{}) {
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Method = 'GET'
    $request.Timeout = 10000
    $request.ReadWriteTimeout = 10000
    $request.AllowAutoRedirect = $false
    $request.KeepAlive = $false
    $request.Proxy = $null
    foreach ($name in $Headers.Keys) {
        $request.Headers.Add([string]$name, [string]$Headers[$name])
    }
    $response = $null
    try {
        $response = $request.GetResponse()
    } catch [System.Net.WebException] {
        if ($null -eq $_.Exception.Response) {
            return @{ Status = 0; Body = $_.Exception.Message }
        }
        $response = $_.Exception.Response
    }
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { $body = $reader.ReadToEnd() } finally { $reader.Dispose() }
        return @{ Status = [int]$response.StatusCode; Body = $body }
    } finally {
        $response.Close()
    }
}

function Get-ProcessTreeIds([int]$RootId) {
    $all = @(Get-CimInstance -ClassName Win32_Process)
    $ids = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootId)
    while ($queue.Count -gt 0) {
        $current = [int]$queue.Dequeue()
        if ($ids -contains $current) { continue }
        $null = $ids.Add($current)
        foreach ($proc in $all) {
            if ([int]$proc.ParentProcessId -eq $current) { $queue.Enqueue([int]$proc.ProcessId) }
        }
    }
    return @($ids)
}

function Stop-ProcessTree([int]$RootId) {
    cmd.exe /c "taskkill /PID $RootId /T /F >nul 2>&1" | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $RootId -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return ($null -eq (Get-Process -Id $RootId -ErrorAction SilentlyContinue))
}

function Find-BackendListener([int]$RootId) {
    $tree = Get-ProcessTreeIds $RootId
    $candidates = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='converter.exe'" |
        Where-Object { $tree -contains [int]$_.ProcessId })
    if ($candidates.Count -eq 0) { return $null }
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
    foreach ($candidate in $candidates) {
        $listener = $listeners | Where-Object { [int]$_.OwningProcess -eq [int]$candidate.ProcessId } | Select-Object -First 1
        if ($null -ne $listener) {
            return @{ Pid = [int]$candidate.ProcessId; Port = [int]$listener.LocalPort }
        }
    }
    return $null
}

function Show-Tail([string]$Path, [string]$Label) {
    $text = Read-SharedText $Path
    if ([string]::IsNullOrWhiteSpace($text)) { return }
    $lines = @($text -split "`r?`n" | Select-Object -Last 15)
    Write-Host "--- $Label (last lines) ---" -ForegroundColor DarkGray
    $lines | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
}

$exitCode = 0
try {
    # --- phase 1: static layout ----------------------------------------------
    Write-Section "Package layout: $App"
    if (-not (Test-Path -LiteralPath $App -PathType Container)) {
        Add-Check 'app directory' 'FAIL' "missing: $App"
    } else {
        Add-Check 'app directory' 'PASS' $App
    }

    $layoutOk = $true
    if (Test-Path -LiteralPath $AppExe -PathType Leaf) {
        Add-Check 'conversor-generator.exe' 'PASS' 'present'
    } else {
        Add-Check 'conversor-generator.exe' 'FAIL' "missing: $AppExe"
        $layoutOk = $false
    }
    if (Test-Path -LiteralPath $BackendExe -PathType Leaf) {
        Add-Check 'resources/backend/converter.exe' 'PASS' 'present'
    } else {
        Add-Check 'resources/backend/converter.exe' 'FAIL' "missing: $BackendExe"
        $layoutOk = $false
    }
    if (Test-Path -LiteralPath $WebDir -PathType Container) {
        $indexHtml = [System.IO.Path]::Combine($WebDir, 'index.html')
        if (Test-Path -LiteralPath $indexHtml -PathType Leaf) {
            Add-Check 'resources/web/index.html' 'PASS' 'present'
        } else {
            Add-Check 'resources/web/index.html' 'FAIL' "missing: $indexHtml"
            $layoutOk = $false
        }
    } else {
        Add-Check 'resources/web/index.html' 'FAIL' "missing directory: $WebDir"
        $layoutOk = $false
    }
    if (Test-Path -LiteralPath ([System.IO.Path]::Combine($Internal, 'python3t.dll')) -PathType Leaf) {
        Add-Check '_internal/python3t.dll' 'PASS' 'present (abi3t shim)'
    } else {
        $cp315 = Test-Path -LiteralPath ([System.IO.Path]::Combine($Internal, 'python315.dll')) -PathType Leaf
        Add-Check '_internal/python3t.dll' 'WARN' ("absent; only matters when cryptography needs the abi3t shim (bundle is cp315=$cp315)")
    }
    Add-Check 'guard source (services/converter/api/desktop_guard.py)' 'INFO' ("present in repository: $GuardInSource")

    if (-not $layoutOk) {
        Add-Check 'runtime phases' 'FAIL' 'skipped because the package layout is incomplete'
    } else {
        # --- phase 2: backend alone ----------------------------------------
        Write-Section 'Backend alone (converter.exe, scratch profile)'
        $Data = [System.IO.Path]::Combine($Scratch, 'userdata')
        New-Item -ItemType Directory -Force -Path $Data | Out-Null
        $outFile = [System.IO.Path]::Combine($Scratch, 'backend.out.txt')
        $errFile = [System.IO.Path]::Combine($Scratch, 'backend.err.txt')
        $bootstrap = New-Secret
        $internalToken = New-Secret
        $jwtSecret = New-Secret

        $backendEnv = @{
            APP_DATA_DIR          = $Data
            WORK_DIR              = [System.IO.Path]::Combine($Data, 'work')
            LOG_DIR               = [System.IO.Path]::Combine($Data, 'logs')
            STORAGE_LOCAL_ROOT    = [System.IO.Path]::Combine($Data, 'storage')
            WEB_DIST_DIR          = $WebDir
            CONVERTER_HOST        = '127.0.0.1'
            CONVERTER_PORT        = '0'
            CONVERTER_QUEUE       = 'inline'
            CONVERTER_OCR_ENABLED = 'false'
            CONVERTER_INTERNAL_TOKEN = $internalToken
            DESKTOP_BOOTSTRAP_SECRET = $bootstrap
            JWT_SECRET            = $jwtSecret
            DESKTOP_HOST_ENABLED  = 'true'
        }
        $previousEnv = Set-ScopedEnv $backendEnv
        try {
            $backend = Start-Process -FilePath $BackendExe -WorkingDirectory $Scratch -WindowStyle Hidden `
                -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        } finally {
            Restore-Env $previousEnv
        }
        $Spawned.Add($backend.Id) | Out-Null

        $port = $null
        $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            $text = Read-SharedText $outFile
            if ($text -match 'CONVERTER_READY (\{[^\r\n]*\})') {
                $payload = $Matches[1] | ConvertFrom-Json
                $port = [int]$payload.port
                break
            }
            if ($backend.HasExited) { break }
            Start-Sleep -Milliseconds 500
        }

        if ($null -eq $port -or $port -le 0) {
            Add-Check 'backend readiness (CONVERTER_READY)' 'FAIL' "no readiness line within $ReadyTimeoutSeconds s (exited=$($backend.HasExited))"
            Show-Tail $outFile 'backend stdout'
            Show-Tail $errFile 'backend stderr'
        } else {
            Add-Check 'backend readiness (CONVERTER_READY)' 'PASS' "port $port"
            $base = "http://127.0.0.1:$port"

            $health = Invoke-Get "$base/health"
            if ($health.Status -eq 200) {
                Add-Check 'GET /health' 'PASS' 'HTTP 200'
            } else {
                Add-Check 'GET /health' 'FAIL' "HTTP $($health.Status)"
            }

            $noHeader = Invoke-Get "$base/"
            if ($noHeader.Status -eq 403) {
                Add-Check 'GET / without x-desktop-host' 'PASS' 'HTTP 403 (host guard active)'
                $wrong = Invoke-Get "$base/" @{ 'x-desktop-host' = (New-Secret) }
                if ($wrong.Status -eq 403) {
                    Add-Check 'GET / with wrong x-desktop-host' 'PASS' 'HTTP 403'
                } else {
                    Add-Check 'GET / with wrong x-desktop-host' 'FAIL' "HTTP $($wrong.Status), expected 403"
                }
            } elseif ($noHeader.Status -eq 200) {
                if ($RequireGuard) {
                    Add-Check 'GET / without x-desktop-host' 'FAIL' 'HTTP 200; host guard is required but not active in this build'
                } else {
                    Add-Check 'GET / without x-desktop-host' 'WARN' ("HTTP 200; host guard not active in this build (guard source present in repository: $GuardInSource). Use -RequireGuard to fail on this.")
                }
            } else {
                Add-Check 'GET / without x-desktop-host' 'FAIL' "HTTP $($noHeader.Status), expected 403"
            }

            $withHeader = Invoke-Get "$base/" @{ 'x-desktop-host' = $bootstrap }
            if ($withHeader.Status -eq 200) {
                Add-Check 'GET / with x-desktop-host' 'PASS' 'HTTP 200 (SPA index)'
            } else {
                Add-Check 'GET / with x-desktop-host' 'FAIL' "HTTP $($withHeader.Status), expected 200"
            }

            $dbFile = [System.IO.Path]::Combine($Data, 'converter.db')
            if (Test-Path -LiteralPath $dbFile -PathType Leaf) {
                Add-Check 'converter.db in APP_DATA_DIR' 'PASS' 'created'
            } else {
                Add-Check 'converter.db in APP_DATA_DIR' 'FAIL' "missing: $dbFile"
            }
        }

        if (Stop-ProcessTree $backend.Id) {
            Add-Check 'backend process tree stopped' 'PASS' "pid $($backend.Id)"
        } else {
            Add-Check 'backend process tree stopped' 'FAIL' "pid $($backend.Id) still running"
        }

        # --- phase 3: desktop shell -----------------------------------------
        Write-Section 'Desktop shell (conversor-generator.exe, --user-data-dir)'
        $alreadyRunning = @(Get-Process -Name 'conversor-generator' -ErrorAction SilentlyContinue)
        if ($alreadyRunning.Count -gt 0) {
            Add-Check 'desktop not already running' 'FAIL' 'conversor-generator is running; close it so the single-instance lock does not end the smoke run early'
        } else {
            $profile = [System.IO.Path]::Combine($Scratch, 'desktop-profile')
            New-Item -ItemType Directory -Force -Path $profile | Out-Null
            $desktopArgs = '--user-data-dir="' + $profile + '"'
            $desktop = Start-Process -FilePath $AppExe -ArgumentList $desktopArgs -WorkingDirectory $App -PassThru
            $Spawned.Add($desktop.Id) | Out-Null

            $listener = $null
            $exitedEarly = $false
            $soakDeadline = (Get-Date).AddSeconds($SoakSeconds)
            while ((Get-Date) -lt $soakDeadline) {
                Start-Sleep -Seconds 1
                if ($desktop.HasExited) { $exitedEarly = $true; break }
                if ($null -eq $listener) {
                    $listener = Find-BackendListener $desktop.Id
                }
            }

            if ($exitedEarly) {
                Add-Check 'desktop alive for soak window' 'FAIL' "process exited with code $($desktop.ExitCode) before $SoakSeconds s"
            } else {
                Add-Check 'desktop alive for soak window' 'PASS' "$SoakSeconds s"
            }

            if ($null -eq $listener) {
                Add-Check 'converter.exe child listening' 'FAIL' 'no converter.exe descendant listening on TCP'
            } else {
                Add-Check 'converter.exe child listening' 'PASS' ("pid $($listener.Pid), port $($listener.Port)")
                $Spawned.Add($listener.Pid) | Out-Null
                $childHealth = Invoke-Get ("http://127.0.0.1:$($listener.Port)/health")
                if ($childHealth.Status -eq 200) {
                    Add-Check 'desktop backend GET /health' 'PASS' 'HTTP 200'
                } else {
                    Add-Check 'desktop backend GET /health' 'FAIL' "HTTP $($childHealth.Status)"
                }
            }

            $profileDb = [System.IO.Path]::Combine($profile, 'converter.db')
            if (Test-Path -LiteralPath $profileDb -PathType Leaf) {
                Add-Check 'profile used by desktop' 'PASS' 'converter.db written under --user-data-dir'
            } else {
                Add-Check 'profile used by desktop' 'WARN' "converter.db not found under --user-data-dir ($profileDb); the flag may not be honored"
            }

            if ($desktop.HasExited) {
                Add-Check 'desktop process tree stopped' 'PASS' 'already exited'
            } elseif (Stop-ProcessTree $desktop.Id) {
                Add-Check 'desktop process tree stopped' 'PASS' "pid $($desktop.Id) and children"
            } else {
                Add-Check 'desktop process tree stopped' 'FAIL' "pid $($desktop.Id) still running"
            }
            if ($null -ne $listener -and $null -ne (Get-Process -Id $listener.Pid -ErrorAction SilentlyContinue)) {
                Stop-ProcessTree $listener.Pid | Out-Null
            }
        }
    }
} catch {
    Add-Check 'unexpected error' 'FAIL' $_.Exception.Message
} finally {
    foreach ($procId in @($Spawned)) {
        if ($null -ne (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            Stop-ProcessTree $procId | Out-Null
        }
    }
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}

# --- summary ----------------------------------------------------------------
Write-Section 'Summary'
$pass = @($Results | Where-Object { $_.Status -eq 'PASS' }).Count
$warn = @($Results | Where-Object { $_.Status -eq 'WARN' }).Count
$fail = @($Results | Where-Object { $_.Status -eq 'FAIL' }).Count
foreach ($row in $Results) {
    Write-Host ("  {0,-4} {1}" -f $row.Status, $row.Check)
}
Write-Host ''
Write-Host "  PASS=$pass WARN=$warn FAIL=$fail  (app: $App)"
if ($fail -gt 0) {
    Write-Host 'SMOKE DESKTOP: FAIL' -ForegroundColor Red
    $exitCode = 1
} else {
    Write-Host 'SMOKE DESKTOP: PASS' -ForegroundColor Green
}
exit $exitCode
