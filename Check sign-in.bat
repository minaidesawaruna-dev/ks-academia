@echo off
title KS Academia - check sign-in
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Could not find the app's Python environment in this folder.
  echo.
  pause
  exit /b 1
)

echo.
echo   Checking the sign-in details on this computer.
echo   Nothing is sent anywhere.
echo.

".venv\Scripts\python.exe" check_sign_in.py

echo.
pause
