Write-Host "Setting up the virtual environment..."
python -m venv .venv
Write-Host "Virtual environment created. Starting virtual environment..."
.venv\Scripts\activate
Write-Host "Installing required packages..."
pip install py-cord
pip install xlsxwriter
pip install xlwings
pip install openpyxl
pip install python-dotenv
pip install xlwings
pip install prompt_toolkit
pip install requests
Write-Host "All packages installed. Deactivating the virtual environment..."
deactivate