@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 쿼터
call "%~dp0_find_python.cmd"
if not defined PY goto nopy
"%PY%" "%~dp0aiquota.py" --watch
pause
exit /b
:nopy
echo Python not found
pause
