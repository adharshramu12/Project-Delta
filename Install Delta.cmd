@echo off
rem Double-click to install (or repair/update) Delta from this folder.
title Installing Delta
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Source "%~dp0."
if errorlevel 1 pause
