@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 (
  echo Python 3.12 x64 not found. Install it and run this file again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm KadroGen.spec
if errorlevel 1 goto failed
if exist "ffmpeg\ffmpeg.exe" if exist "ffmpeg\ffprobe.exe" (
  if not exist "dist\KadroGen\ffmpeg" mkdir "dist\KadroGen\ffmpeg"
  copy /y "ffmpeg\ffmpeg.exe" "dist\KadroGen\ffmpeg\" >nul
  copy /y "ffmpeg\ffprobe.exe" "dist\KadroGen\ffmpeg\" >nul
)
echo Built dist\KadroGen\KadroGen.exe
echo If ffmpeg is missing, copy the ffmpeg folder from your original KadroGen release.
pause
exit /b 0
:failed
echo Build failed. Read the error above.
pause
exit /b 1
