@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   Сборка digest.exe — подожди 2-3 минуты
echo ============================================
echo.

echo [0/2] Проверяю Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ОШИБКА: Python не найден!
    echo Скачай и установи с сайта: python.org
    echo.
    pause
    exit /b 1
)

echo [1/2] Устанавливаю нужные программы...
python -m pip install httpx beautifulsoup4 lxml pyinstaller --trusted-host pypi.org --trusted-host files.pythonhosted.org --quiet
if errorlevel 1 (
    echo.
    echo ОШИБКА при установке зависимостей.
    echo.
    pause
    exit /b 1
)

echo [2/2] Собираю .exe ...
python -m PyInstaller --onefile --name digest --add-data "keywords.py;." --add-data "cities.py;." scraper.py
if errorlevel 1 (
    echo.
    echo ОШИБКА при сборке .exe — покажи текст выше.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
if exist dist\digest.exe (
    echo   ГОТОВО!
    echo.
    echo   Файл: dist\digest.exe
    echo.
    echo   Просто дважды кликни на него —
    echo   он сам соберёт дайджест и
    echo   откроет браузер с результатом.
    echo.
    echo   Никаких API и паролей не нужно.
) else (
    echo   Что-то пошло не так.
    echo   Покажи мне текст ошибки выше.
)
echo ============================================
echo.
pause
