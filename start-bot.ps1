# Run the bot using the venv's Python directly (no activation needed, and avoids
# the `py` launcher picking system Python over the venv).
Write-Host "Starting the bot..."
.\discord-bot-venv\Scripts\python.exe main.py
