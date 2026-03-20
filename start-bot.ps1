Write-Host "Activating the virtual environment..."
.venv\Scripts\activate
Write-Host "Starting the bot..."
python main.py
Write-Host "Deactivating the virtual environment..."
deactivate