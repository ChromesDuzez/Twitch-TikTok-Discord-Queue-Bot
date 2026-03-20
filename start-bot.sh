#!/bin/sh
echo "Activating the virtual environment..."
source .venv/bin/activate
echo "Starting the bot..."
python main.py
echo "Deactivating the virtual environment..."
deactivate