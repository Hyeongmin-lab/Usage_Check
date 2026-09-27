@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_find_python.cmd"
if not defined PY goto nopy
start "" "%PYW%" "%~dp0aiquota.py" --widget
exit /b
:nopy
echo Python not found - python.org 에서 Python 3 설치 (Add to PATH 체크)
pause
