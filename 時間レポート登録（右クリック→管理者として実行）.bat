@echo off
PowerShell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process PowerShell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File ""C:\TradingTask\register_hourly_report.ps1""' -Verb RunAs"
