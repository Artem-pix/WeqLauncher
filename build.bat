@echo off
rem Сборка WeqLauncher.exe (запускать на Windows, нужен Python 3.10+)
python -m pip install -r requirements.txt pyinstaller || goto :error
pyinstaller --noconfirm --clean --onefile --windowed --name WeqLauncher --collect-all minecraft_launcher_lib weqlauncher.py || goto :error
echo.
echo Готово: dist\WeqLauncher.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
