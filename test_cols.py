"""追加指標のカラム名テスト"""
import requests, json, config

headers = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Referer": "https://www.tradingview.com/",
    "Origin": "https://www.tradingview.com",
    "Cookie": f"sessionid={config.SESSION_ID}",
}

def fmt(v): return f"{v:.4f}" if isinstance(v, float) else str(v)

candidates = [
    "EMA200[1]",    # EMA200の1日前（傾き計算用）
    "EMA200[5]",    # EMA200の5日前
    "ATR",          # ATR(14)
    "ATR[14]",      # ATR 期間14
    "ATR14",        # ATR 期間14 別形式
    "volume",       # 当日出来高
    "Value.Traded", # 売買代金
    "high",         # 当日高値
    "low",          # 当日安値
    "Stoch.K",      # ストキャス
    "W.R14",        # Williams %R
    "MACD.macd",    # MACD
    "EMA50",        # EMA50
    "EMA20",        # EMA20
    "Pivot.M.Classic.Middle", # ピボット
]

print(f"{'カラム名':<35} {'状態':>6}  {'TSE:7203 値':>15}")
print("-" * 65)

for col in candidates:
    payload = {
        "filter": [],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": []}, "tickers": ["TSE:7203"]},
        "columns": ["name", "close", col],
        "range": [0, 1],
    }
    r = requests.post("https://scanner.tradingview.com/japan/scan",
                      headers=headers, data=json.dumps(payload), timeout=10)
    resp = r.json()
    if r.status_code == 200:
        d = (resp.get("data") or [{}])[0].get("d", [])
        val = d[2] if len(d) > 2 else None
        print(f"  {col:<33}  OK    {fmt(val):>15}")
    else:
        print(f"  {col:<33}  NG    {resp.get('error','')[:30]:>15}")
