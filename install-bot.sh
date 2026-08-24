#!/bin/sh
# Create the virtual environment and install dependencies into it.
echo "Setting up the virtual environment (discord-bot-venv)..."
python3.12 -m venv discord-bot-venv
echo "Upgrading pip..."
./discord-bot-venv/bin/python -m pip install --upgrade pip
echo "Installing required packages from requirements.txt..."
./discord-bot-venv/bin/python -m pip install -r requirements.txt
echo "Done. Run ./start-bot.sh to launch the bot."
