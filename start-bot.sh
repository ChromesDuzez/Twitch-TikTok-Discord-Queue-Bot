#!/bin/sh
echo "Activating the virtual environment..."
source ./discord-bot-venv/bin/activate
echo "Starting the bot..."
python main.py
echo "Deactivating the virtual environment..."
deactivate