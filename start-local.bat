@echo off
setlocal
cd /d "%~dp0"

set "BACKEND=%~dp0backend"
set "FRONTEND=%~dp0frontend"

REM ---- First run only: create the backend venv and install Python deps ----
if not exist "%BACKEND%\.venv\Scripts\python.exe" (
  echo [setup] Creating backend virtual environment and installing dependencies...
  python -m venv "%BACKEND%\.venv"
  "%BACKEND%\.venv\Scripts\python.exe" -m pip install -r "%BACKEND%\requirements.txt"
)

REM ---- First run only: install frontend deps ----
if not exist "%FRONTEND%\node_modules" (
  echo [setup] Installing frontend dependencies. First run may take a minute...
  pushd "%FRONTEND%"
  call npm install
  popd
)

REM ---- Warn if the Claude key isn't in the environment ----
if "%ANTHROPIC_API_KEY%"=="" echo [warn] ANTHROPIC_API_KEY not set - the Claude evaluation step will be skipped.

REM ---- Optional: run a triage pass first  (usage: start-local.bat --run) ----
if /i "%~1"=="--run" (
  echo [run] Running triage now - fetching emails and evaluating with Claude...
  pushd "%BACKEND%"
  .venv\Scripts\python -m app.triage.run --hours 24
  popd
)

REM ---- Launch both servers, each in its own window ----
start "Triage Backend"  /d "%BACKEND%"  cmd /k .venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
start "Triage Frontend" /d "%FRONTEND%" cmd /k npm run dev

REM ---- Open the app once the dev server has had a moment to start ----
timeout /t 5 >nul
start "" http://localhost:5173

endlocal
