@echo off
chcp 65001 >nul
rem Yiyan (忆言) - double click to launch (ASCII only, avoid codepage issues)
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.8+ and check "Add python.exe to PATH".
  pause
  exit /b 1
)
python "%~dp0llm_chat.py" %*
if errorlevel 1 pause
