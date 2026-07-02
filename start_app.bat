@echo off
setlocal enabledelayedexpansion

:: 1. Verifica se python no PATH e valido (e nao o alias da Windows Store)
where python >nul 2>nul
if %errorlevel% equ 0 (
    python --version 2>&1 | findstr "3." >nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=python"
        goto :run
    )
)

:: 2. Verifica pastas de instalacao do Python do usuario
set "USER_PY_DIR=%LOCALAPPDATA%\Programs\Python"
if exist "!USER_PY_DIR!" (
    for /d %%d in ("!USER_PY_DIR!\Python*") do (
        if exist "%%d\python.exe" (
            set "PYTHON_EXE=%%d\python.exe"
            goto :run
        )
    )
)

:: 3. Verifica pastas de instalacao globais do Python
set "SYSTEM_PY_DIR=%ProgramFiles%\Python"
if exist "!SYSTEM_PY_DIR!" (
    for /d %%d in ("!SYSTEM_PY_DIR!\Python*") do (
        if exist "%%d\python.exe" (
            set "PYTHON_EXE=%%d\python.exe"
            goto :run
        )
    )
)

:: 4. Fallback direto para o Python 3.11 instalado
if exist "C:\Users\Rocha\AppData\Local\Programs\Python\Python311\python.exe" (
    set "PYTHON_EXE=C:\Users\Rocha\AppData\Local\Programs\Python\Python311\python.exe"
    goto :run
)

echo Erro: Python 3 nao foi encontrado no sistema.
echo Instale o Python ou adicione-o ao PATH e tente novamente.
pause
exit /b 1

:run
echo Usando Python de: !PYTHON_EXE!
"!PYTHON_EXE!" "%~dp0run_server.py"
