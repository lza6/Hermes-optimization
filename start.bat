@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo    DecoTV Manager - Auto Start Script
echo ========================================
echo.

REM Set virtual environment directory
set VENV_DIR=venv
REM set PYTHON_SCRIPT=decotv_gui.py -> Changed to module run or main.py
set MODULE_NAME=hermes.main
set REQUIREMENTS_FILE=requirements.txt

REM Step 1: Check Python installation
echo [1/4] Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not detected!
    echo.
    echo Please install Python 3.8 or higher first
    echo Download: https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during installation
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [SUCCESS] Python %PYTHON_VERSION% detected
echo.

REM Step 2: Check or create virtual environment
echo [2/4] Checking virtual environment...
if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [INFO] Virtual environment already exists
) else (
    echo [INFO] Creating virtual environment...
    python -m venv %VENV_DIR%
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment!
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created
)
echo.

REM Activate virtual environment
call "%VENV_DIR%\Scripts\activate.bat"

REM Step 3: Check and install dependencies
echo [3/4] Checking dependencies...
if exist "%REQUIREMENTS_FILE%" (
    echo [INFO] Installing/updating dependencies...
    pip install -r "%REQUIREMENTS_FILE%" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if %errorlevel% neq 0 (
        echo [WARNING] Some dependencies failed to install, trying official source...
        pip install -r "%REQUIREMENTS_FILE%"
    )
    echo [SUCCESS] Dependencies installed
) else (
    echo [WARNING] %REQUIREMENTS_FILE% not found
    echo [INFO] Installing core dependencies directly...
    pip install fastapi uvicorn httpx pydantic loguru -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo.

REM Step 4: Start Server
echo [4/4] Starting Server...
echo ========================================
echo    Starting Hermes Gateway...
echo ========================================
echo.

echo Dashboard available at http://localhost:8000/dashboard
echo.

REM Auto open browser after 3 seconds
start /b cmd /c "timeout /t 3 >nul & start http://localhost:8000/dashboard"

python -m %MODULE_NAME%

REM If program exits abnormally
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Program exited abnormally, error code: %errorlevel%
    echo.
    pause
)

endlocal
