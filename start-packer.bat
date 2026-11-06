@echo off
REM ---------------------------------------------------------------------------
REM Start the offline bundle packer Web UI (http://127.0.0.1:8765).
REM
REM PORTABLE: this script contains NO machine-specific paths. Probe order:
REM   PY_EXE override -> python / python3 on PATH -> "py -3" Windows launcher
REM   -> common per-user / system Python install folders.
REM A candidate counts only if it really executes Python code: we ask it to
REM exit with code 42. The Microsoft Store alias stub exits 0 doing nothing,
REM so it is skipped. (Capturing stdout via FOR /F is unreliable here because
REM CMD mis-parses a quoted exe path followed by a quoted -c argument.)
REM
REM The primary entry point is still:  python packer.py
REM packer.py uses only the Python 3.9+ standard library, so that command works
REM on any machine with a real Python. This .bat is just a Windows fallback for
REM the case where "python" resolves to the silent Store alias stub.
REM
REM Extra args are forwarded, e.g. start-packer.bat --port 9000 --no-browser
REM Set PY_EXE to force one interpreter. Set PACKER_NO_PAUSE=1 for CI use.
REM Keep this file ASCII-only: the project path may contain non-ASCII characters.
REM ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY="
set "PYARGS="

if defined PY_EXE if exist "%PY_EXE%" set "PY=%PY_EXE%"

REM 1) PATH candidates
if not defined PY (
  for %%N in (python python3) do (
    if not defined PY (
      for /f "delims=" %%I in ('where %%N 2^>nul') do (
        if not defined PY (
          set "CAND=%%~I"
          "!CAND!" -c "import sys;sys.exit(42)" >nul 2>nul
          if errorlevel 42 if not errorlevel 43 set "PY=!CAND!"
        )
      )
    )
  )
)

REM 2) Windows Python launcher (installed with CPython, even when PATH is off)
if not defined PY (
  py -3 -c "import sys;sys.exit(42)" >nul 2>nul
  if errorlevel 42 if not errorlevel 43 (
    set "PY=py"
    set "PYARGS=-3"
  )
)

REM 3) Python installed but not on PATH (common per-user / system locations)
if not defined PY (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*"
                 "%ProgramFiles%\Python3*"
                 "%ProgramFiles(x86)%\Python3*"
                 "C:\Python3*") do (
    if not defined PY if exist "%%~D\python.exe" (
      set "CAND=%%~D\python.exe"
      "!CAND!" -c "import sys;sys.exit(42)" >nul 2>nul
      if errorlevel 42 if not errorlevel 43 set "PY=!CAND!"
    )
  )
)

if not defined PY (
  echo [FAIL] No working Python 3.9+ interpreter found.
  echo        Install Python from https://www.python.org/downloads/ and tick
  echo        "Add python.exe to PATH", or run: set PY_EXE=C:\path\to\python.exe
  echo        Note: on Windows "python" can be a silent Microsoft Store alias stub.
  if not defined PACKER_NO_PAUSE pause
  exit /b 1
)

echo [OK] Python: %PY% %PYARGS%
echo [OK] Opening http://127.0.0.1:8765 (Ctrl+C to stop)
"%PY%" %PYARGS% packer.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [FAIL] packer.py exited with code %RC%.
exit /b %RC%
