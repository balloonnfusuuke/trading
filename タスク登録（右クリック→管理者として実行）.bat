@echo off
chcp 65001 > nul
title TradingView タスク登録

echo ================================================
echo  TradingView スクリーニング タスク登録
echo ================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "C:\TradingTask\register.ps1"

echo.
pause
