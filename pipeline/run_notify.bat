@echo off
cd /d "%~dp0"
python notify.py >> logs\notify.log 2>&1
