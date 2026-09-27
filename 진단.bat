@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 쿼터 진단
call "%~dp0_find_python.cmd"
if not defined PY goto nopy
echo 사용할 Python: %PY%
"%PY%" "%~dp0aiquota.py" --doctor
pause
exit /b
:nopy
echo Python not found
pause
