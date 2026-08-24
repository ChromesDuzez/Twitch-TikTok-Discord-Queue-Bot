# Create the virtual environment and install dependencies into it.
Write-Host "Setting up the virtual environment (discord-bot-venv)..."
py -3.12 -m venv discord-bot-venv
Write-Host "Upgrading pip..."
.\discord-bot-venv\Scripts\python.exe -m pip install --upgrade pip
Write-Host "Installing required packages from requirements.txt..."
.\discord-bot-venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "Done. Run .\start-bot.ps1 to launch the bot."
