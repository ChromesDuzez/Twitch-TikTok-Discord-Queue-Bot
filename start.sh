#!/bin/bash
echo "pip install py-cord"
pip3 install py-cord 
echo "pip install xlsxwriter"
pip3 install xlsxwriter
echo "pip install openpyxl"
pip3 install openpyxl
echo "pip install python-dotenv"
pip3 install python-dotenv

read -p "Press enter to continue..." -n 1 -s 

python3 main.py

read -p "Press enter to continue..." -n 1 -s 
