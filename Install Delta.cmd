@echo off
rem Double-click to install (or repair/update) Delta from this folder.
title Installing Delta
if not exist "%~dp0scripts\install.ps1" (
    echo.
    echo Delta's files are not next to this file. If you opened the zip from GitHub, extract it first:
    echo right-click the zip, choose "Extract All...", then double-click "Install Delta.cmd" in the extracted folder.
    echo.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Source "%~dp0."
if errorlevel 1 pause
