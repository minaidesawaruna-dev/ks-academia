@echo off
title KS Academia
cd /d "%~dp0"

if not exist ".venv\Scripts\streamlit.exe" (
  echo.
  echo   Could not find the app's Python environment in this folder.
  echo   Expected: %CD%\.venv\Scripts\streamlit.exe
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting KS Academia...
echo   Your browser will open in a moment.
echo.
echo   Keep this window open while you work.
echo   Closing it shuts the app down.
echo.

".venv\Scripts\streamlit.exe" run app.py

echo.
echo   KS Academia has stopped.
pause
