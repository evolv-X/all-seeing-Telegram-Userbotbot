@echo off
cd /d %~dp0

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate

if exist requirements.txt (
    echo Installing wheel for faster installing...
    pip install wheel
    echo Installing dependencies...
    pip install -r requirements.txt
    echo. > venv\Lib\site-packages\installed
) else (
    echo requirements.txt not found, skipping dependency installation.
)

cls
echo Starting Script...
python main.py

pause
