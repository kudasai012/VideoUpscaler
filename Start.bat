@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Видео-Апскейлер · улучшение качества до 4K

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo.
    echo  [ОШИБКА] Python не найден.
    echo.
    echo  1. Скачай Python:  https://www.python.org/downloads/windows/
    echo  2. При установке ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH"
    echo  3. Запусти этот файл снова.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0tools\ffmpeg.exe" (
    where ffmpeg >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  Подсказка: FFmpeg не найден. Программа сама предложит скачать его
        echo  при запуске (пункт меню 3). Ручной способ - в README.md,
        echo  раздел "Установка компонентов".
        echo.
    )
)

%PY% "%~dp0upscaler.py" %*
echo.
pause
endlocal
