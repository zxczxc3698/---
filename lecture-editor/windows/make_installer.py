"""도구 전체를 품은 설치 파일 하나를 만든다. 받아서 두 번 누르면 끝나게."""
import base64, io, zipfile, textwrap
from pathlib import Path

SRC = Path("/home/user/---/lecture-editor")
FILES = ["edit_lecture.py", "batch.py", "terms.txt", "chapters.txt", "README.md",
         "windows/helper.py", "windows/usage.txt", "windows/edit.bat"]

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in FILES:
        z.write(SRC / rel, rel)
payload = base64.b64encode(buf.getvalue()).decode()
lines = "\n".join(textwrap.wrap(payload, 200))

bat = r'''@echo off
chcp 65001 >nul
title Lecture Editor - Installer
setlocal
rem Desktop is often redirected into OneDrive. Fall back if missing.
set "DESK=%USERPROFILE%\Desktop"
if not exist "%DESK%" set "DESK=%USERPROFILE%\OneDrive\Desktop"
if not exist "%DESK%" set "DESK=%USERPROFILE%"
set "HOME_DIR=%DESK%\lecture-editor"

echo.
echo   ==========================================
echo     Lecture Editor - installer
echo   ==========================================
echo.
echo   Installing to: %HOME_DIR%
echo.

echo   [1/4] Unpacking tool files ...
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText('%~f0'); $i=$t.LastIndexOf('#PAYLOAD#'); $b=$t.Substring($i+9) -replace '\s',''; [IO.File]::WriteAllBytes($env:TEMP+'\le.zip',[Convert]::FromBase64String($b)); Expand-Archive -Path ($env:TEMP+'\le.zip') -DestinationPath '%HOME_DIR%' -Force; Remove-Item ($env:TEMP+'\le.zip')"
if errorlevel 1 goto failed
if not exist "%HOME_DIR%\edit_lecture.py" goto failed
echo         done.

where winget >nul 2>&1
if errorlevel 1 (
  echo.
  echo   This Windows has no 'winget'. Install these two by hand, then run again:
  echo     Python   https://www.python.org/downloads/
  echo              CHECK the box "Add Python to PATH" while installing
  echo     ffmpeg   https://www.gyan.dev/ffmpeg/builds/
  echo.
  echo   Tool files are already unpacked at:
  echo     %HOME_DIR%
  pause
  exit /b
)

echo   [2/4] Checking Python ...
where python >nul 2>&1
if errorlevel 1 (
  echo         not found - installing, this takes a few minutes ...
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  echo.
  echo   ------------------------------------------------------------
  echo     Python installed.
  echo     CLOSE this window, then run this file again.
  echo   ------------------------------------------------------------
  pause
  exit /b
)
echo         ok.

echo   [3/4] Checking ffmpeg ...
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo         not found - installing, this takes a few minutes ...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo   ------------------------------------------------------------
  echo     ffmpeg installed.
  echo     CLOSE this window, then run this file again.
  echo   ------------------------------------------------------------
  pause
  exit /b
)
echo         ok.

echo   [4/4] Installing speech recognition ...
python -m pip install --quiet --upgrade pip >nul 2>&1
python -m pip install --quiet faster-whisper
if errorlevel 1 (
  echo         FAILED - see the messages above.
  pause
  exit /b
)
echo         done.

echo.
python "%HOME_DIR%\windows\helper.py" --check
echo.
echo   ------------------------------------------------------------
echo     Folder: %HOME_DIR%
echo     To edit videos: run  windows\edit.bat  in that folder
echo   ------------------------------------------------------------
echo.

explorer "%HOME_DIR%\windows"
pause
exit /b

:failed
echo.
echo   Unpacking failed. Tell Claude what this window says.
pause
exit /b

#PAYLOAD#
'''
bat = bat.replace("\n", "\r\n") + lines.replace("\n", "\r\n") + "\r\n"
out = Path("/tmp/claude-0/-home-user----/cff9a196-3d36-5fba-8df5-e5f0ace46650/scratchpad/install.bat")
out.write_text(bat, encoding="ascii")
print(f"만든 파일: {out}  {len(bat)/1024:.0f} KB")
