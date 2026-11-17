@echo off
chcp 65001 >nul
echo === Полная запись курса «Испанский для падела» v4 (27 глав + Режим Тень + iCloud/Google Drive) ===
if "%GEMINI_API_KEY%"=="" (
    set /p GEMINI_API_KEY="Вставь GEMINI_API_KEY и нажми Enter: "
)
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python validate_dsl.py
if errorlevel 1 pause & exit /b 1
python build_course.py --out-dir dist_audio_v4 --with-shadowing
echo.
echo Полный комплект MP3 собран в папке dist_audio_v4 и автоматически скопирован в iCloudDrive / Google Drive (если папки подключены на ПК).
pause
