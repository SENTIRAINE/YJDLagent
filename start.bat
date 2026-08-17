@echo off
setlocal EnableExtensions

cd /d "%~dp0"

if not exist ".env" (
    if not defined AGENT_STORAGE_BACKEND set "AGENT_STORAGE_BACKEND=mongodb"
    if not defined AGENT_MONGODB_URI set "AGENT_MONGODB_URI=mongodb://127.0.0.1:27017/?replicaSet=rs0"
    if not defined AGENT_MONGODB_DATABASE set "AGENT_MONGODB_DATABASE=yjdl_agent"
    if not defined AGENT_CHECKPOINT_BACKEND set "AGENT_CHECKPOINT_BACKEND=mongodb"
    if not defined AGENT_CHECKPOINT_MONGODB_DATABASE set "AGENT_CHECKPOINT_MONGODB_DATABASE=yjdl_agent"
    if not defined AGENT_MAP_RESULT_LIMIT set "AGENT_MAP_RESULT_LIMIT=50"
    if not defined AGENT_WORKER_ENABLED set "AGENT_WORKER_ENABLED=true"
)

if /I "%~1"=="worker" (
    set "AGENT_WORKER_ENABLED=true"
    goto worker
)
if /I "%~1"=="api" (
    set "AGENT_WORKER_ENABLED=false"
    shift
)

set "APP_MODULE=app.main:app"
set "APP_HOST=0.0.0.0"
set "APP_PORT=8000"

if defined LANGGRAPH_HOST set "APP_HOST=%LANGGRAPH_HOST%"
if defined LANGGRAPH_PORT set "APP_PORT=%LANGGRAPH_PORT%"
if not "%~1"=="" set "APP_PORT=%~1"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found: "%PYTHON_EXE%"
    echo Create it with: python -m venv .venv
    exit /b 2
)

"%PYTHON_EXE%" -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uvicorn is not installed in .venv.
    echo Install dependencies with: .venv\Scripts\python.exe -m pip install -e ".[dev]"
    exit /b 3
)

set "YJDL_PROJECT_ROOT=%CD%"
set "YJDL_PYTHON=%PYTHON_EXE%"
set "YJDL_PORT=%APP_PORT%"

echo [INFO] Checking for an existing YJDL LangGraph service on port %APP_PORT%...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=[IO.Path]::GetFullPath($env:YJDL_PROJECT_ROOT).TrimEnd('\'); $python=[IO.Path]::GetFullPath($env:YJDL_PYTHON); $portValue=0; if(-not [int]::TryParse($env:YJDL_PORT,[ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535){ Write-Host '[ERROR] Port must be an integer from 1 to 65535.'; exit 20 }; $portPattern='--port(?:=|\s+)\"?'+[regex]::Escape([string]$portValue)+'\"?(?:\s|$)'; $all=@(Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'uvicorn.exe'\"); $roots=@($all | Where-Object { $command=$_.CommandLine; if(-not $command){ return $false }; $samePython=$_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $python); $mentionsRoot=$command.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0; ($samePython -or $mentionsRoot) -and $command -match 'app\.main:app' -and $command -match $portPattern }); $ownedIds=[Collections.Generic.HashSet[int]]::new(); $roots | ForEach-Object { [void]$ownedIds.Add([int]$_.ProcessId) }; do { $added=$false; foreach($process in $all){ if($ownedIds.Contains([int]$process.ParentProcessId) -and $ownedIds.Add([int]$process.ProcessId)){ $added=$true } } } while($added); $owned=@($all | Where-Object { $ownedIds.Contains([int]$_.ProcessId) }); if($owned.Count -gt 0){ Write-Host ('[INFO] Stopping existing project process(es): '+(($owned.ProcessId | Sort-Object) -join ', ')); Stop-Process -Id @($owned.ProcessId) -Force -ErrorAction Stop; $deadline=(Get-Date).AddSeconds(10); do { $alive=@($owned | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }); if($alive.Count -eq 0){ break }; Start-Sleep -Milliseconds 200 } while((Get-Date) -lt $deadline); if($alive.Count -gt 0){ throw 'The existing project process did not stop within 10 seconds.' } }; $listeners=@(Get-NetTCPConnection -State Listen -LocalPort $portValue -ErrorAction SilentlyContinue); if($listeners.Count -gt 0){ $pids=(($listeners.OwningProcess | Sort-Object -Unique) -join ', '); Write-Host ('[ERROR] Port '+$portValue+' is occupied by another process (PID '+$pids+'). It was not stopped.'); exit 21 }"

if errorlevel 1 (
    echo [ERROR] Restart preflight failed. The service was not started.
    exit /b 4
)

echo [INFO] Starting %APP_MODULE% on http://%APP_HOST%:%APP_PORT%
if exist ".env" (
    echo [INFO] Runtime backends and worker mode will be loaded from .env.
) else (
    echo [INFO] Storage=%AGENT_STORAGE_BACKEND% Checkpoint=%AGENT_CHECKPOINT_BACKEND% WorkerEnabled=%AGENT_WORKER_ENABLED%
)
echo [INFO] Press Ctrl+C to stop the service.

if exist ".env" (
    echo [INFO] Loading environment variables from .env
    "%PYTHON_EXE%" -m uvicorn %APP_MODULE% --host "%APP_HOST%" --port "%APP_PORT%" --env-file ".env"
) else (
    "%PYTHON_EXE%" -m uvicorn %APP_MODULE% --host "%APP_HOST%" --port "%APP_PORT%"
)

set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo [ERROR] Service exited with code %APP_EXIT_CODE%.
exit /b %APP_EXIT_CODE%

:worker
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found: "%PYTHON_EXE%"
    echo Create it with: python -m venv .venv
    exit /b 2
)

"%PYTHON_EXE%" -c "import app.cli.agent_worker, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Agent Worker dependencies are not installed correctly.
    echo Install dependencies with: .venv\Scripts\python.exe -m pip install -e ".[dev]"
    exit /b 3
)

echo [INFO] Starting leased Agent Worker. Storage=%AGENT_STORAGE_BACKEND% Checkpoint=%AGENT_CHECKPOINT_BACKEND%
echo [INFO] Press Ctrl+C to stop it.
if exist ".env" (
    echo [INFO] Loading environment variables from .env
    "%PYTHON_EXE%" -m app.cli.agent_worker --env-file ".env"
) else (
    "%PYTHON_EXE%" -m app.cli.agent_worker
)
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo [ERROR] Agent Worker exited with code %APP_EXIT_CODE%.
exit /b %APP_EXIT_CODE%
