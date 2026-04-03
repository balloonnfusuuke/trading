@echo off
chcp 65001 > nul
echo 日本株 1時間ごとレポートを開始します...
echo このウィンドウは最小化してください（閉じると止まります）
echo.
python -c "
import time, subprocess, sys
from datetime import datetime

def in_market():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 540 <= t <= 930

print('監視開始。9:00-15:00の間、1時間おきにDiscordに送ります。')
sys.stdout.flush()

while True:
    if in_market():
        subprocess.run(['python', 'monitor.py', '--report'], cwd=r'C:\Users\Owner\OneDrive\ドキュメント\Trading')
        print(f'送信完了: {datetime.now().strftime(\"%H:%M\")}')
        sys.stdout.flush()
        time.sleep(3600)
    else:
        print(f'取引時間外 ({datetime.now().strftime(\"%H:%M\")}) - 9:00まで待機中...')
        sys.stdout.flush()
        time.sleep(300)
"
pause
