Write-Host "Setting up the virtual environment..."
py -3.12 -m venv discord-bot-venv
Write-Host "Virtual environment created. Starting virtual environment..."
.\discord-bot-venv\Scripts\activate
Write-Host "Installing required packages..."
pip install -r requirements.txt
Write-Host "Packages installed. Upgrading pip..."
py -m pip install --upgrade pip
Write-Host "All packages installed. Deactivating the virtual environment..."
deactivate