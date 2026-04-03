@echo off
chcp 65001 > nul

:: 管理者権限チェック → なければ自動で昇格リクエスト
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 管理者権限が必要です。UAC ダイアログが開きます...
    powershell -Command "Start-Process cmd -Verb RunAs -ArgumentList '/c \"%~f0\"'"
    exit /b
)

echo ================================================
echo  TradingView スクリーニング タスク登録
echo ================================================

set PYTHON=C:\Users\Owner\AppData\Local\Programs\Python\Python311\python.exe
set SCRIPT_DIR=C:\Users\Owner\OneDrive\ドキュメント\Trading
set SCRIPT="%SCRIPT_DIR%\main.py"

:: ── 既存タスクの削除（再登録用） ──
schtasks /delete /tn "TradingView_US_Screening" /f >nul 2>&1
schtasks /delete /tn "TradingView_JP_Screening" /f >nul 2>&1

:: ── 米国株タスク（毎朝 4:40、スリープ解除あり） ──
schtasks /create ^
  /tn "TradingView_US_Screening" ^
  /tr "\"%PYTHON%\" %SCRIPT% --us-now" ^
  /sc DAILY ^
  /st 04:40 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /it ^
  /f
if %errorlevel% equ 0 (
    echo [OK] 米国株タスク登録完了: 毎朝 04:40
) else (
    echo [NG] 米国株タスク登録失敗
)

:: ── 日本株タスク（毎日 15:10、スリープ解除あり） ──
schtasks /create ^
  /tn "TradingView_JP_Screening" ^
  /tr "\"%PYTHON%\" %SCRIPT% --jp-now" ^
  /sc DAILY ^
  /st 15:10 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /it ^
  /f
if %errorlevel% equ 0 (
    echo [OK] 日本株タスク登録完了: 毎日 15:10
) else (
    echo [NG] 日本株タスク登録失敗
)

:: ── スリープ解除オプションをXMLで追加 ──
echo スリープ解除設定を適用中...

schtasks /query /tn "TradingView_US_Screening" /xml ONE > "%TEMP%\task_us.xml" 2>nul
powershell -Command "(Get-Content '%TEMP%\task_us.xml') -replace '<WakeToRun>false</WakeToRun>','<WakeToRun>true</WakeToRun>' | Set-Content '%TEMP%\task_us_wake.xml'"
powershell -Command "if (-not (Select-String -Path '%TEMP%\task_us.xml' -Pattern 'WakeToRun' -Quiet)) { (Get-Content '%TEMP%\task_us.xml') -replace '</Settings>','<WakeToRun>true</WakeToRun></Settings>' | Set-Content '%TEMP%\task_us_wake.xml' } else { Copy-Item '%TEMP%\task_us.xml' '%TEMP%\task_us_wake.xml' -Force }"
schtasks /delete /tn "TradingView_US_Screening" /f >nul 2>&1
schtasks /create /tn "TradingView_US_Screening" /xml "%TEMP%\task_us_wake.xml" /f >nul 2>&1

schtasks /query /tn "TradingView_JP_Screening" /xml ONE > "%TEMP%\task_jp.xml" 2>nul
powershell -Command "if (-not (Select-String -Path '%TEMP%\task_jp.xml' -Pattern 'WakeToRun' -Quiet)) { (Get-Content '%TEMP%\task_jp.xml') -replace '</Settings>','<WakeToRun>true</WakeToRun></Settings>' | Set-Content '%TEMP%\task_jp_wake.xml' } else { (Get-Content '%TEMP%\task_jp.xml') -replace '<WakeToRun>false</WakeToRun>','<WakeToRun>true</WakeToRun>' | Set-Content '%TEMP%\task_jp_wake.xml' }"
schtasks /delete /tn "TradingView_JP_Screening" /f >nul 2>&1
schtasks /create /tn "TradingView_JP_Screening" /xml "%TEMP%\task_jp_wake.xml" /f >nul 2>&1

echo.
echo ================================================
echo  登録完了！以下のスケジュールで自動実行されます:
echo    米国株: 毎朝 04:40  → LINE 通知
echo    日本株: 毎日 15:10  → LINE 通知
echo  ※ PC がスリープ中でも自動で起動します
echo ================================================
echo.
pause
