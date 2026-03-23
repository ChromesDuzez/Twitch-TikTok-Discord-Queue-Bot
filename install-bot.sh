#!/bin/sh
echo "Setting up the virtual environment..."
python3.12 -m venv .venv
echo "Virtual environment created. Starting virtual environment..."
source .venv/bin/activate
echo "Installing required packages..."
pip install -r requirements.txt
echo "Packages installed. Upgrading pip..."
pip install --upgrade pip
echo "All packages installed. Deactivating the virtual environment..."
deactivate