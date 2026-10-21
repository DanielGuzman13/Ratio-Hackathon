@echo off
setlocal
title Ratio - servidor
cd /d "%~dp0"

REM 1) Encontrar Python: PATH, lanzador py o Anaconda
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY if exist "%USERPROFILE%\Anaconda3\python.exe" set "PY=%USERPROFILE%\Anaconda3\python.exe"
if not defined PY (
  echo No se encontro Python. Instalalo desde https://www.python.org/downloads/ y vuelve a ejecutar.
  pause
  exit /b 1
)
echo Usando Python: %PY%

REM 2) Configuracion y entorno virtual
if not exist .env copy .env.example .env >nul
if not exist .venv\Scripts\python.exe "%PY%" -m venv .venv
set "VPY=%~dp0.venv\Scripts\python.exe"

echo Instalando dependencias...
"%VPY%" -m pip install -q --disable-pip-version-check -r backend\requirements-dev.txt
if errorlevel 1 ( echo Fallo la instalacion de dependencias. & pause & exit /b 1 )

REM 3) Pruebas y servidor
cd backend
echo Ejecutando pruebas...
"%VPY%" -m pytest -q
echo.
echo Abriendo http://localhost:8000 ...
start "" cmd /c "timeout /t 5 >nul & start http://localhost:8000"
"%VPY%" -m uvicorn app.main:app --port 8000
pause
