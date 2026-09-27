@echo off
rem  PY = console python, PYW = windowless python (widget)
set "PY="
set "PYW="
set "CONDA_DIR="
where py >nul 2>nul && set "PY=py" && set "PYW=pyw"
if defined PY goto :eof
for %%D in ("%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\anaconda3" "%USERPROFILE%\miniconda3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\anaconda3") do (
  if not defined PY (
    if exist "%%~D\python.exe" (
      set "PY=%%~D\python.exe"
      set "CONDA_DIR=%%~D"
    )
  )
)
if defined CONDA_DIR set "PATH=%CONDA_DIR%;%CONDA_DIR%\Library\bin;%CONDA_DIR%\Scripts;%PATH%"
if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined PY set "PY=%%I"
  )
)
if not defined PY goto :eof
for %%F in ("%PY%") do set "PYW=%%~dpFpythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
