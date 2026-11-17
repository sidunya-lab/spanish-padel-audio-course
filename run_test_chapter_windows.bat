@echo off
chcp 65001 >nul
echo === Шаг 1: Сборка тестовой главы (2 минуты) на прослушивание ===
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
python build_course.py --only-test --out-dir dist_test_chapter
echo.
echo Готово! Прослушай файл в папке dist_test_chapter\chapters\
echo Если темп, ударения и голоса нравятся — запускай run_build_windows.bat для записи всего курса!
pause
