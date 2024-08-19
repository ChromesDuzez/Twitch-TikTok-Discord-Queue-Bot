#!/bin/bash
echo "pip install py-cord"
pip install py-cord
echo "pip install xlsxwriter"
pip install xlsxwriter
echo "pip install openpyxl"
pip install openpyxl
echo "pip install python-dotenv"
pip install python-dotenv

read -p "Press enter to continue..." -n 1 -s 

python3 main.py

read -p "Press enter to continue..." -n 1 -s 
