@echo off
rem ---------------------------------------------------------------
rem  AhutDining launcher
rem  NOTE: keep this file ASCII-only. cmd.exe parses .bat with the
rem  OEM code page (GBK on zh-CN Windows), so Chinese text inside
rem  a parenthesized IF block breaks the parser. Chinese messages
rem  are printed by run.py instead.
rem ---------------------------------------------------------------
chcp 65001 >nul
title AhutDining - AHUT Campus Meal Recommender
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [ERROR] Python was not found on this computer.
  echo         Install Python 3.9+ from https://www.python.org/downloads/
  echo         and tick "Add Python to PATH" during setup.
  pause
  exit /b 1
)

%PY% run.py
if errorlevel 1 (
  echo.
  echo [ERROR] Startup failed. Send the message above to the developer.
  pause
)
