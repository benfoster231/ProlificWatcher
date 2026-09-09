@echo off
setlocal

echo Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 goto :error

echo Registering system Chrome with Playwright (no download if Chrome is already installed)...
python -m playwright install chrome
if errorlevel 1 goto :error

echo Building ProlificWatcher.exe...
REM The excludes below matter if your Python environment has other large
REM packages installed for unrelated projects (numpy, pandas, PyQt5, etc.) -
REM sentry-sdk's PyInstaller hook eagerly hidden-imports every integration
REM it knows how to auto-detect, which sweeps in whatever's importable on
REM your machine even though none of it is actually used here. Without these
REM excludes the build can balloon from ~40MB to 150MB+.
pyinstaller --onefile --console --name ProlificWatcher --collect-all playwright watcher.py ^
    --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
    --exclude-module matplotlib --exclude-module PyQt5 --exclude-module PySide2 ^
    --exclude-module PySide6 --exclude-module lxml --exclude-module PIL ^
    --exclude-module cryptography --exclude-module tiktoken
if errorlevel 1 goto :error

echo Copying config.json next to the built exe...
copy /Y config.json dist\config.json >nul

echo.
echo Done. Your program is at: dist\ProlificWatcher.exe
echo dist\config.json sits next to it - edit that to change filters, then run the exe.
goto :eof

:error
echo.
echo Build failed. See the error above.
exit /b 1
