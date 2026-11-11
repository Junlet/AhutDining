@echo off
chcp 65001 >nul
title 安工大校园用餐推荐
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 没有找到 python 命令。
  echo 请先安装 Python 3.9 及以上版本，安装时勾选 "Add Python to PATH"。
  echo 下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)

python run.py
if errorlevel 1 (
  echo.
  echo 启动失败，请把上面的报错信息发给开发者。
  pause
)
