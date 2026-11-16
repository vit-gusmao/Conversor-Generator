@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Conversor Generator - inicializador
cd /d "%~dp0"

set "ROOT=%CD%"
set "CONVERTER=%ROOT%\services\converter"
set "WEB_DIST=%ROOT%\apps\web\dist\index.html"
set "PY=%CONVERTER%\.venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8000"
set "URL=http://%HOST%:%PORT%"
set "HEALTH=%URL%/health"

echo ============================================================
echo   Conversor Generator  -  servidor local
echo ============================================================
echo.

if not exist "%CONVERTER%\api\main.py" (
    echo [ERRO] Estrutura do projeto nao encontrada em:
    echo        %CONVERTER%
    echo.
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [ERRO] Ambiente virtual Python nao encontrado:
    echo        %PY%
    echo.
    echo Crie o ambiente com:
    echo    cd services\converter
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -e ".[test]"
    echo.
    pause
    exit /b 1
)

call :check_ocr
call :probe
if not errorlevel 1 (
    echo O servidor ja esta ativo em %URL%
    echo Abrindo a pagina...
    start "" "%URL%"
    exit /b 0
)

netstat -ano | findstr /R /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [ERRO] A porta %PORT% esta em uso por outro processo.
    echo        Feche esse processo ou altere PORT neste script.
    echo.
    pause
    exit /b 1
)

if not exist "%WEB_DIST%" (
    echo [aviso] A interface ainda nao foi compilada.
    where npm >nul 2>&1
    if errorlevel 1 (
        echo         npm nao encontrado: a API funcionara, mas sem interface web.
        echo.
    ) else (
        echo         Compilando a interface. Isso pode levar alguns minutos...
        pushd "%ROOT%\apps\web"
        call npm run build
        set "BUILD_RC=!errorlevel!"
        popd
        if not "!BUILD_RC!"=="0" (
            echo [aviso] A compilacao falhou. A API continuara disponivel.
            echo.
        )
    )
)

echo Iniciando o servidor...
start "Conversor Generator - servidor" /min /d "%CONVERTER%" "%PY%" -m uvicorn api.main:app --host %HOST% --port %PORT% --log-level info

echo Aguardando o servidor responder...
set /a TRIES=0

:wait
call :probe
if not errorlevel 1 goto ready
set /a TRIES+=1
if !TRIES! GEQ 90 goto timeout
timeout /t 1 /nobreak >nul
goto wait

:ready
echo Servidor pronto.
echo Abrindo a pagina no navegador...
start "" "%URL%"
echo.
echo ============================================================
echo   Aplicacao : %URL%
echo   Docs API  : %URL%/docs
echo   Servidor  : janela "Conversor Generator - servidor"
echo   Para parar: feche essa janela ou use Ctrl+C nela.
echo ============================================================
echo.
exit /b 0

:timeout
echo.
echo [ERRO] O servidor nao respondeu em 90 segundos.
echo        Abra a janela "Conversor Generator - servidor" para ver o erro.
echo.
pause
exit /b 1

:probe
where curl >nul 2>&1
if errorlevel 1 goto probe_ps
curl -s -o nul --max-time 2 "%HEALTH%" >nul 2>&1
if errorlevel 1 (exit /b 1)
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

:probe_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%HEALTH%'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
if errorlevel 1 (exit /b 1)
exit /b 0
