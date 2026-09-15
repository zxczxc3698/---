@echo off
chcp 65001 >nul
title Lecture Editor
cd /d "%~dp0"
python "%~dp0helper.py" %*
pause
