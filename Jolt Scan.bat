@echo off
title Jolt Scan
cd /d "%~dp0app"
if not exist "tscli\target\release\tscli.exe" (
  python setup_scanner.py
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
python server.py
if errorlevel 1 pause
