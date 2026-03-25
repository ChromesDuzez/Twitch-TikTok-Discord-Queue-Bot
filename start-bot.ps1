Write-Host "Activating the virtual environment..."
.\discord-bot-venv\Scripts\activate
Write-Host "Starting the bot..."
py -3.12 main.py
Write-Host "Deactivating the virtual environment..."
deactivate