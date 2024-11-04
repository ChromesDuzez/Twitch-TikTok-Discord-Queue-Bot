#!/bin/bash
echo "pip install py-cord"
pip install py-cord --break-system-packages
echo "pip install xlsxwriter"
pip install xlsxwriter --break-system-packages
echo "pip install openpyxl"
pip install openpyxl --break-system-packages
echo "pip install python-dotenv"
pip install python-dotenv --break-system-packages

read -p "Press enter to continue..." -n 1 -s 

python3 main.py

read -p "Press enter to continue..." -n 1 -s 
