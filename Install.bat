@echo off
rem EverEvolving Trading Agent -- double-click to install on Windows.
title EverEvolving Trading Agent - installing
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\windows\install.ps1" %*
if errorlevel 1 (
    echo.
    echo   Something went wrong. The message above says what happened.
    echo   Nothing was changed outside %%USERPROFILE%%\EverEvolving.
    pause
    exit /b 1
)
timeout /t 6 >nul
