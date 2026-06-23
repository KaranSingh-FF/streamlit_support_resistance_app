@echo off
REM Build the S/R desktop terminal into a standalone Windows app.
REM Run from the repo root:  packaging\build_exe.bat
setlocal
cd /d "%~dp0\.."

echo Installing build dependencies...
python -m pip install -r requirements-dev.txt || goto :error

echo Building SR-Terminal.exe (this can take a few minutes)...
pyinstaller --noconfirm --clean packaging\desktop.spec || goto :error

echo.
echo Done. Launch:  dist\SR-Terminal\SR-Terminal.exe
goto :eof

:error
echo.
echo Build FAILED. See the output above.
exit /b 1
