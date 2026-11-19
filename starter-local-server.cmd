@echo off
rem ============================================================
rem  Conversor Generator - inicializador local (Windows)
rem
rem  Uso:  starter-local-server.cmd [--no-browser] [--rebuild-web]
rem
rem  Variaveis de ambiente opcionais (lidas do processo, nao do .env):
rem    CONVERTER_PORT  porta do motor (padrao 8000)
rem    CG_VENV_DIR     pasta do ambiente virtual (padrao services\converter\.venv)
rem    CG_NOPAUSE=1    nao aguarda tecla em caso de erro (uso automatizado)
rem
rem  Etapas: porta -> OCR (informativo) -> Python/venv/dependencias ->
rem  interface web (apps\web\dist) -> servidor uvicorn -> navegador.
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
title Conversor Generator - inicializador
cd /d "%~dp0"

set "ROOT=%CD%"
set "CONVERTER=%ROOT%\services\converter"
set "PYPROJECT=%CONVERTER%\pyproject.toml"
set "WEB_DIR=%ROOT%\apps\web"
set "CONTRACTS_DIR=%ROOT%\packages\contracts"
set "WEB_INDEX=%WEB_DIR%\dist\index.html"

set "VENV=%CONVERTER%\.venv"
if defined CG_VENV_DIR set "VENV=%CG_VENV_DIR%"
set "PYV=%VENV%\Scripts\python.exe"
set "STAMP_FILE=%VENV%\cg-deps.stamp"
set "LOG=%VENV%\install.log"
rem Log do servidor uvicorn (stdout+stderr), mesmo diretorio de dados do app.
set "SERVER_LOG=%CONVERTER%\data\logs\server.log"
rem Extras necessarios no modo standalone: ocr (fallback RapidOCR/Tesseract) e
rem migrate (alembic, para atualizar um banco SQLite existente).
set "EXTRAS=ocr,migrate"

set "HOST=127.0.0.1"
set "PORT=%CONVERTER_PORT%"
if not defined PORT set "PORT=8000"
set "URL=http://%HOST%:%PORT%"
set "HEALTH=%URL%/health"

set "OPT_NOBROWSER=0"
set "OPT_REBUILD=0"
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-browser" set "OPT_NOBROWSER=1"
if /i "%~1"=="--rebuild-web" set "OPT_REBUILD=1"
shift
goto parse_args
:args_done

echo.
echo ============================================================
echo   Conversor Generator  -  servidor local
echo ============================================================
echo.

if exist "%CONVERTER%\api\main.py" goto project_ok
echo [ERRO] Estrutura do projeto nao encontrada em:
echo        %CONVERTER%
goto fail
:project_ok

echo %PORT%| findstr /R "^[0-9][0-9]*$" >nul
if errorlevel 1 goto bad_port
goto port_valid
:bad_port
echo [ERRO] CONVERTER_PORT invalido: %PORT%
goto fail
:port_valid

call :probe
if not errorlevel 1 goto already_running

netstat -ano | findstr /R /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 goto port_busy
goto port_free

:already_running
echo O servidor ja esta ativo em %URL%
echo Abrindo a pagina...
if "%OPT_NOBROWSER%"=="0" start "" "%URL%"
exit /b 0

:port_busy
echo [ERRO] A porta %PORT% esta em uso por outro processo.
echo        Feche esse processo ou defina outra porta em CONVERTER_PORT.
goto fail

:port_free
call :check_ocr

echo [1/3] Ambiente Python e dependencias...
call :venv_is_ok
if not errorlevel 1 goto venv_ready
if not exist "%VENV%\" goto make_venv
echo        Ambiente existente nao funciona ou usa Python abaixo de 3.11. Recriando...
rmdir /s /q "%VENV%" >nul 2>&1
if exist "%VENV%\" goto err_rmdir

:make_venv
call :ensure_python
if errorlevel 1 goto err_no_python
echo        Criando o ambiente em %VENV% ...
"%PYEXE%" -m venv "%VENV%"
if errorlevel 1 goto err_venv
call :venv_is_ok
if errorlevel 1 goto err_venv
:venv_ready

call :hash_pyproject
set "WANT=%PYPROJECT_HASH% %EXTRAS%"
set "HAVE="
if exist "%STAMP_FILE%" set /p "HAVE="<"%STAMP_FILE%"
if not defined PYPROJECT_HASH goto install_deps
if not "!HAVE!"=="!WANT!" goto install_deps
"%PYV%" -c "import fastapi, uvicorn, sqlalchemy, openpyxl, tzdata" >nul 2>&1
if errorlevel 1 goto install_deps
echo        Dependencias ja instaladas (pyproject.toml sem alteracoes).
goto deps_ok

:install_deps
echo        Instalando dependencias. A primeira vez pode levar varios minutos...
echo        Log completo: %LOG%
if exist "%STAMP_FILE%" del "%STAMP_FILE%" >nul 2>&1
"%PYV%" -m pip install --disable-pip-version-check --upgrade pip >"%LOG%" 2>&1
"%PYV%" -m pip install --disable-pip-version-check -e "%CONVERTER%[%EXTRAS%]" >>"%LOG%" 2>&1
if errorlevel 1 goto err_install
"%PYV%" -c "import fastapi, uvicorn, sqlalchemy, openpyxl, tzdata" >>"%LOG%" 2>&1
if errorlevel 1 goto err_install
<nul set /p "=%WANT%" >"%STAMP_FILE%"
echo        Dependencias instaladas.

:deps_ok
echo.
echo [2/3] Interface web...
if "%OPT_REBUILD%"=="1" goto build_web_now
if exist "%WEB_INDEX%" goto web_ok
echo        Interface ainda nao compilada. Compilando apps\web ...
:build_web_now
call :build_web
if errorlevel 1 goto web_missing
:web_ok
echo        Interface pronta.
goto start_server
:web_missing
echo [aviso] A interface web nao esta disponivel. A API sera iniciada sem a pagina.
echo         Para tentar compilar de novo: starter-local-server.cmd --rebuild-web
echo.

:start_server
echo.
echo [3/3] Iniciando o servidor em %URL% ...
rem Agendamentos precisam do executor ligado neste processo. Respeita um valor ja definido.
if not defined AUTOMATION_EXECUTOR_ENABLED set "AUTOMATION_EXECUTOR_ENABLED=true"
rem stdout+stderr do uvicorn tambem vao para server.log (append). PYTHONUNBUFFERED evita perda de linhas no crash.
if not exist "%CONVERTER%\data\logs\" mkdir "%CONVERTER%\data\logs"
echo. >>"%SERVER_LOG%"
echo ==== inicio %DATE% %TIME% ==== >>"%SERVER_LOG%"
set "PYTHONUNBUFFERED=1"
start "Conversor Generator - servidor" /min /d "%CONVERTER%" cmd /c ""%PYV%" -m uvicorn api.main:app --host %HOST% --port %PORT% --log-level info >>"%SERVER_LOG%" 2>&1"
echo        Log do servidor: %SERVER_LOG%
echo        Aguardando o servidor responder (ate 90 segundos)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u = '%HEALTH%'; $sw = [Diagnostics.Stopwatch]::StartNew(); while ($sw.Elapsed.TotalSeconds -lt 90) { try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $u; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; Start-Sleep -Milliseconds 1000 }; exit 1"
if errorlevel 1 goto err_timeout

echo        Servidor pronto.
echo.
echo ============================================================
echo   Aplicacao : %URL%
echo   Docs API  : %URL%/docs
echo   Servidor  : janela "Conversor Generator - servidor"
echo   Log       : %SERVER_LOG%
echo   Para parar: feche essa janela ou use Ctrl+C nela.
echo ============================================================
if "%OPT_NOBROWSER%"=="0" start "" "%URL%"
echo.
exit /b 0

rem ------------------------------------------------------------
rem Erros (sempre via goto)
rem ------------------------------------------------------------
:err_rmdir
echo [ERRO] Nao foi possivel remover o ambiente antigo: %VENV%
echo        Feche processos que usem essa pasta e tente de novo.
goto fail

:err_no_python
echo [ERRO] Python 3.11 ou superior nao foi encontrado.
echo        Instale o Python 3.12 em https://www.python.org/downloads/
echo        marcando "Add python.exe to PATH" e execute este arquivo de novo.
goto fail

:err_venv
echo [ERRO] Nao foi possivel criar o ambiente virtual em %VENV%
echo        Verifique se o Python instalado inclui o modulo venv.
goto fail

:err_install
echo [ERRO] Falha ao instalar as dependencias Python.
echo        Ultimas linhas de %LOG%:
echo ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%LOG%' -Tail 40"
echo ------------------------------------------------------------
goto fail

:err_timeout
echo.
echo [ERRO] O servidor nao respondeu em 90 segundos.
echo        Log do servidor: %SERVER_LOG%
echo        Ultimas 30 linhas do log:
echo ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path -LiteralPath '%SERVER_LOG%') { Get-Content -LiteralPath '%SERVER_LOG%' -Tail 30 } else { Write-Host '(log ainda nao foi criado)' }"
echo ------------------------------------------------------------
echo        Abra a janela "Conversor Generator - servidor" para ver o erro.
goto fail

:fail
echo.
if not defined CG_NOPAUSE pause
exit /b 1

rem ------------------------------------------------------------
rem Sub-rotinas
rem ------------------------------------------------------------

rem Sonda /health. Errorlevel 0 somente com HTTP 200.
:probe
where curl.exe >nul 2>&1
if errorlevel 1 goto probe_ps
curl.exe -f -s -o nul --max-time 2 "%HEALTH%"
exit /b %errorlevel%
:probe_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%HEALTH%'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
exit /b %errorlevel%

rem Verifica se o venv responde e usa Python 3.11 ou superior.
:venv_is_ok
if not exist "%PYV%" exit /b 1
"%PYV%" -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
exit /b %errorlevel%

rem Localiza um Python >= 3.11 e define PYEXE (caminho completo).
:find_python
set "PYEXE="
call :try_py 3.12
if defined PYEXE exit /b 0
call :try_py 3.13
if defined PYEXE exit /b 0
call :try_py 3.11
if defined PYEXE exit /b 0
call :try_py 3.14
if defined PYEXE exit /b 0
call :try_py 3.15
if defined PYEXE exit /b 0
call :try_py 3
if defined PYEXE exit /b 0
call :try_path_python
if defined PYEXE exit /b 0
exit /b 1

:try_py
set "CAND="
for /f "usebackq delims=" %%P in (`py -%~1 -c "import sys;print(sys.executable)" 2^>nul`) do set "CAND=%%P"
if not defined CAND exit /b 1
call :try_exe "%CAND%"
exit /b 0

:try_path_python
for /f "usebackq delims=" %%P in (`where python 2^>nul`) do if not defined PYEXE call :consider_path "%%P"
exit /b 0

rem Ignora o alias da Microsoft Store (WindowsApps), que nao e um Python real.
:consider_path
echo %~1| findstr /I "WindowsApps" >nul && exit /b 1
call :try_exe "%~1"
exit /b 0

:try_exe
"%~1" -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYEXE=%~1"
exit /b 0

rem Procura Python; se nao houver e o winget existir, instala o 3.12 no usuario.
:ensure_python
call :find_python
if not errorlevel 1 exit /b 0
where winget >nul 2>&1
if errorlevel 1 exit /b 1
echo        Python 3.11 ou superior nao encontrado. Instalando o Python 3.12 com winget...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" call :try_exe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PYEXE exit /b 0
call :find_python
exit /b %errorlevel%

rem Calcula o SHA256 de pyproject.toml (sem espacos) em PYPROJECT_HASH.
:hash_pyproject
set "PYPROJECT_HASH="
for /f "skip=1 usebackq tokens=*" %%H in (`certutil -hashfile "%PYPROJECT%" SHA256 2^>nul`) do if not defined PYPROJECT_HASH set "PYPROJECT_HASH=%%H"
set "PYPROJECT_HASH=%PYPROJECT_HASH: =%"
exit /b 0

rem GLM-OCR: informativo, nunca bloqueia a inicializacao.
:check_ocr
echo Verificando o motor de OCR (GLM-OCR no Ollama)...
where ollama >nul 2>&1
if errorlevel 1 goto ocr_missing
ollama list >"%TEMP%\pdf2x_ollama.txt" 2>nul
if errorlevel 1 goto ocr_down
findstr /I /C:"glm-ocr" "%TEMP%\pdf2x_ollama.txt" >nul 2>&1
if errorlevel 1 goto ocr_nomodel
del "%TEMP%\pdf2x_ollama.txt" >nul 2>&1
echo         GLM-OCR pronto. Paginas digitalizadas serao reconhecidas na GPU.
echo.
exit /b 0

:ocr_missing
echo [aviso] Ollama nao encontrado no PATH.
echo         Instale em https://ollama.com e rode: ollama pull glm-ocr:latest
echo         Sem ele, paginas digitalizadas nao serao reconhecidas.
echo.
exit /b 0

:ocr_down
del "%TEMP%\pdf2x_ollama.txt" >nul 2>&1
echo [aviso] O Ollama esta instalado mas nao respondeu.
echo         Inicie com: ollama serve   (ou abra o app do Ollama)
echo.
exit /b 0

:ocr_nomodel
del "%TEMP%\pdf2x_ollama.txt" >nul 2>&1
echo [aviso] O modelo glm-ocr nao esta instalado no Ollama.
echo         Rode: ollama pull glm-ocr:latest
echo.
exit /b 0

rem Node.js >= 20.19 ou >= 22 (requisito do Vite 7). Define NODE_MAJOR/NODE_MINOR.
:node_ok
set "NODE_MAJOR=0"
set "NODE_MINOR=0"
for /f "usebackq tokens=1,2 delims=v." %%A in (`node -v 2^>nul`) do set "NODE_MAJOR=%%A" & set "NODE_MINOR=%%B"
echo !NODE_MAJOR!| findstr /R "^[0-9][0-9]*$" >nul
if errorlevel 1 exit /b 1
if "!NODE_MINOR!"=="" set "NODE_MINOR=0"
if !NODE_MAJOR! GEQ 22 exit /b 0
if !NODE_MAJOR! EQU 20 if !NODE_MINOR! GEQ 19 exit /b 0
exit /b 1

:ensure_node
call :node_ok
if not errorlevel 1 exit /b 0
if exist "%ProgramFiles%\nodejs\node.exe" set "PATH=%ProgramFiles%\nodejs;%PATH%"
call :node_ok
if not errorlevel 1 exit /b 0
where winget >nul 2>&1
if errorlevel 1 goto node_missing
echo        Node.js 20.19+ (ou 22+) nao encontrado. Instalando o Node.js LTS com winget...
winget install -e --id OpenJS.NodeJS.LTS --scope user --accept-package-agreements --accept-source-agreements
if exist "%ProgramFiles%\nodejs\node.exe" set "PATH=%ProgramFiles%\nodejs;%PATH%"
call :node_ok
if not errorlevel 1 exit /b 0
:node_missing
echo [aviso] Node.js 20.19+ indisponivel. A interface web nao sera compilada.
exit /b 1

rem Compila contracts e depois o web (npm ci na primeira vez).
:build_web
call :ensure_node
if errorlevel 1 exit /b 1
call :npm_step "%CONTRACTS_DIR%"
if errorlevel 1 exit /b 1
call :npm_step "%WEB_DIR%"
exit /b %errorlevel%

:npm_step
pushd "%~1"
if errorlevel 1 exit /b 1
if exist "node_modules\" goto npm_build
echo        Instalando dependencias de %~1 ...
if exist "package-lock.json" goto npm_ci
call npm install --no-audit --no-fund
goto npm_install_check
:npm_ci
call npm ci --no-audit --no-fund
:npm_install_check
if errorlevel 1 goto npm_fail
:npm_build
echo        Compilando %~1 ...
call npm run build
if errorlevel 1 goto npm_fail
popd
exit /b 0
:npm_fail
popd
exit /b 1
