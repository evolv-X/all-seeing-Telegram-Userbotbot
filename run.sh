#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "Installing wheel for faster installing..."
    pip install wheel
    echo "Installing dependencies..."
    pip install -r requirements.txt
else
    echo "requirements.txt not found, skipping dependency installation."
fi

clear
echo "Starting Script..."
python main.py

read -p "Press Enter to exit..."
