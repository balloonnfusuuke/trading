"""
hourly_report.pyw
─────────────────────────────────────────────────────────────
日本株 1時間ごとポジションレポートを Discord に送信する常駐スクリプト。

.pyw 拡張子 = ダブルクリックで黒い画面が出ずにバックグラウンド実行される。

動作仕様:
  - 9:00 / 10:00 / 11:00 / 12:00 / 13:00 / 14:00 / 15:00 に送信
  - 取引時間外・土日はスキップ
  - PCが起動している間ずっと動き続ける

起動方法: このファイルをダブルクリック（タスクトレイには表示されない）
停止方法: タスクマネージャー → pythonw.exe を終了
"""

import time
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MONITOR    = str(SCRIPT_DIR / "monitor.py")
PYTHON     = sys.executable  # 現在使っているPython

def should_send() -> bool:
    """現在が送信すべき時刻か（毎時00分±1分、取引時間内、平日）を判定する。"""
    now = datetime.now()
    if now.weekday() >= 5:                   # 土日
        return False
    h = now.hour
    m = now.minute
    if h < 9 or h > 15:                      # 取引時間外
        return False
    if h == 15 and m > 5:                    # 15:05以降はスキップ
        return False
    return m <= 1                            # 毎時 00〜01分 に送信

sent_hours = set()   # 同じ時間に二重送信しないよう管理

while True:
    now = datetime.now()
    key = (now.date(), now.hour)

    if should_send() and key not in sent_hours:
        subprocess.run(
            [PYTHON, MONITOR, "--report"],
            cwd=str(SCRIPT_DIR),
            creationflags=0x08000000,    # CREATE_NO_WINDOW
        )
        sent_hours.add(key)
        # 古い記録を削除（メモリ節約）
        today = now.date()
        sent_hours = {k for k in sent_hours if k[0] == today}

    time.sleep(30)   # 30秒ごとに時刻チェック
