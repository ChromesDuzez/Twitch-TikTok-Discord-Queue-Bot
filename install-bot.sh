#!/bin/sh
echo "Setting up the virtual environment..."
python3 -m venv .venv
echo "Virtual environment created. Starting virtual environment..."
source .venv/bin/activate
echo "Installing required packages..."
pip install -r requirements.txt
echo "All packages installed. Deactivating the virtual environment..."
deactivate