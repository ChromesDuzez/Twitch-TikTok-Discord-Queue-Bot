#!/bin/bash
echo "pip install py-cord"
pip install py-cord
echo "pip install xlsxwriter"
pip install xlsxwriter
echo "pip install xlwings"
pip install xlwings
echo "pip install openpyxl"
pip install openpyxl
echo "pip install python-dotenv"
pip install python-dotenv

read -p "Press enter to continue..." -n 1 -s 

python main.py

read -p "Press enter to continue..." -n 1 -s 
