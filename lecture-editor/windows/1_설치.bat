@echo off
chcp 65001 >nul
title Lecture Editor - Setup
cd /d "%~dp0"
echo.
echo   ============================================
echo     Lecture Editor - first time setup
echo   ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   [1/3] Installing Python ...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo   Python installed. CLOSE this window and run 1_설치.bat again.
  pause
  exit /b
) else (
  echo   [1/3] Python - already installed
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo   [2/3] Installing ffmpeg ...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo   ffmpeg installed. CLOSE this window and run 1_설치.bat again.
  pause
  exit /b
) else (
  echo   [2/3] ffmpeg - already installed
)

echo   [3/3] Installing speech recognition ...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet faster-whisper
if errorlevel 1 (
  echo.
  echo   FAILED. See 사용법.txt
  pause
  exit /b
)

echo.
python "%~dp0도우미.py" --check
echo.
pause
