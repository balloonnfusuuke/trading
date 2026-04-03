# ============================================================
# タスク登録スクリプト
# 右クリック → "PowerShell で実行" または "管理者として実行"
# ============================================================
chcp 65001 | Out-Null

$pythonPath = "C:\Users\Owner\AppData\Local\Programs\Python\Python311\python.exe"
$scriptDir  = "C:\Users\Owner\OneDrive\ドキュメント\Trading"
$mainScript = Join-Path $scriptDir "main.py"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  TradingView スクリーニング タスク登録" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ── 米国株タスク（毎朝 4:40、スリープ解除あり） ──
$actionUS  = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$mainScript`" --us-now" `
    -WorkingDirectory $scriptDir

$triggerUS = New-ScheduledTaskTrigger -Daily -At "04:40"

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DisallowStartIfOnBatteries:$false `
    -StopIfGoingOnBatteries:$false

Register-ScheduledTask `
    -TaskName   "TradingView_US_Screening" `
    -Action     $actionUS `
    -Trigger    $triggerUS `
    -Settings   $settings `
    -Description "米国株 Connors RSI(2) スクリーニング（毎朝 4:40）" `
    -RunLevel   Highest `
    -Force | Out-Null

Write-Host "[OK] 米国株タスク登録: 毎朝 04:40" -ForegroundColor Green

# ── 日本株タスク（毎日 15:10、スリープ解除あり） ──
$actionJP = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$mainScript`" --jp-now" `
    -WorkingDirectory $scriptDir

$triggerJP = New-ScheduledTaskTrigger -Daily -At "15:10"

Register-ScheduledTask `
    -TaskName   "TradingView_JP_Screening" `
    -Action     $actionJP `
    -Trigger    $triggerJP `
    -Settings   $settings `
    -Description "日本株 Connors RSI(2) スクリーニング（毎日 15:10）" `
    -RunLevel   Highest `
    -Force | Out-Null

Write-Host "[OK] 日本株タスク登録: 毎日 15:10" -ForegroundColor Green

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  登録完了！" -ForegroundColor Green
Write-Host "  米国株: 毎朝 04:40  → LINE 通知" -ForegroundColor White
Write-Host "  日本株: 毎日 15:10  → LINE 通知" -ForegroundColor White
Write-Host "  ※ PC がスリープ中でも自動で起動します" -ForegroundColor Yellow
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# 登録確認
$taskUS = Get-ScheduledTask -TaskName "TradingView_US_Screening" -ErrorAction SilentlyContinue
$taskJP = Get-ScheduledTask -TaskName "TradingView_JP_Screening" -ErrorAction SilentlyContinue

if ($taskUS) { Write-Host "確認 米国株タスク: $($taskUS.State)" -ForegroundColor Green }
else         { Write-Host "確認 米国株タスク: 登録失敗" -ForegroundColor Red }

if ($taskJP) { Write-Host "確認 日本株タスク: $($taskJP.State)" -ForegroundColor Green }
else         { Write-Host "確認 日本株タスク: 登録失敗" -ForegroundColor Red }

Write-Host ""
Read-Host "Enterキーで閉じる"
