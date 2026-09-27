@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 쿼터 대시보드
call "%~dp0_find_python.cmd"
if not defined PY goto nopy
"%PY%" "%~dp0aiquota.py" --web
pause
exit /b
:nopy
echo Python not found - python.org 에서 Python 3 설치 (Add to PATH 체크)
pause
