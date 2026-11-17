@echo off
REM Convenience launcher for Windows.
cd /d "%~dp0"
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
python main.py %*
pause
