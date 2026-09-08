@echo off
setlocal

echo Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 goto :error

echo Registering system Chrome with Playwright (no download if Chrome is already installed)...
python -m playwright install chrome
if errorlevel 1 goto :error

echo Building ProlificWatcher.exe...
pyinstaller --onefile --console --name ProlificWatcher --collect-all playwright watcher.py
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
