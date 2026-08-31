@echo off
title KS Academia - set up sign-in
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Could not find the app's Python environment in this folder.
  echo.
  pause
  exit /b 1
)

echo.
echo   This creates the sign-in details for KS Academia.
echo   Nothing is sent anywhere - it all stays on this computer.
echo.

".venv\Scripts\python.exe" make_password_hash.py

echo.
pause
