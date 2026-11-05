@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

pyinstaller --noconfirm --onefile --windowed --name "PDFStampTool" --icon icon.ico stamp_tool.py
if errorlevel 1 goto fail

echo.
echo 打包完成: dist\PDFStampTool.exe
pause
exit /b 0

:fail
echo.
echo 打包失败，请检查上面的错误信息。
pause
exit /b 1
