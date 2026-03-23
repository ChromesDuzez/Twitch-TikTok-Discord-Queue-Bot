Write-Host "Setting up the virtual environment..."
python -m venv .venv
Write-Host "Virtual environment created. Starting virtual environment..."
.venv\Scripts\activate
Write-Host "Installing required packages..."
pip install -r requirements.txt
Write-Host "All packages installed. Deactivating the virtual environment..."
deactivate