#!/bin/sh
echo "Setting up the virtual environment..."
python3 -m venv .venv
echo "Virtual environment created. Starting virtual environment..."
source .venv/bin/activate
echo "Installing required packages..."
pip install py-cord
pip install xlsxwriter
pip install xlwings
pip install openpyxl
pip install python-dotenv
pip install xlwings
pip install prompt_toolkit
pip install requests
echo "All packages installed. Deactivating the virtual environment..."
deactivate