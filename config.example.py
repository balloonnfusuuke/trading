# ============================================================
# config.example.py  —  これを config.py にコピーして値を埋めて使う
#   copy config.example.py config.py   (PowerShell: Copy-Item)
# ============================================================

import os

# --- TradingView 認証 ---
_tv = (os.environ.get("TV_SESSION_ID") or "").strip()
SESSION_ID = _tv or "YOUR_TRADINGVIEW_SESSION_ID"

# ============================================================
# Discord Webhook
# ============================================================
_wh = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
DISCORD_WEBHOOK_URL = _wh or "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN"


# ============================================================
# 共通カラム定義（US・日本共通）
# ============================================================
COLUMNS = [
    "name",
    "close",
    "RSI2",
    "EMA200",
    "EMA200[1]",
    "EMA150",
    "EMA50",
    "EMA20",
    "ATR",
    "Stoch.K",
    "volume",
    "average_volume_30d_calc",
    "high",
    "low",
    "market_cap_basic",
    "SMA200",
]

COLUMN_LABELS = {
    "name":                    "Ticker",
    "close":                   "現在値",
    "RSI2":                    "RSI(2)",
    "EMA200":                  "EMA200",
    "EMA200[1]":               "EMA200前日",
    "EMA150":                  "EMA150",
    "EMA50":                   "EMA50",
    "EMA20":                   "EMA20",
    "ATR":                     "ATR",
    "Stoch.K":                 "Stoch.K",
    "volume":                  "出来高",
    "average_volume_30d_calc": "平均出来高30日",
    "high":                    "高値",
    "low":                     "安値",
    "market_cap_basic":        "時価総額",
    "SMA200":                  "SMA200",
}

US_MARKET = {
    "name":         "米国株",
    "flag":         "🇺🇸",
    "url":          "https://scanner.tradingview.com/america/scan",
    "schedule_hour":   4,
    "schedule_minute": 40,
    "cap_unit":        1_000_000_000,
    "cap_unit_label":  "B USD",
    "filters": [
        {"left": "average_volume_30d_calc", "operation": "greater", "right": 1_000_000},
        {"left": "RSI2",                    "operation": "less",    "right": 10},
        {"left": "EMA200",                  "operation": "less",    "right": "close"},
        {"left": "EMA200",                  "operation": "eless",   "right": "EMA150"},
        {"left": "market_cap_basic",        "operation": "greater", "right": 2_000_000_000},
    ],
    "criteria": [
        "平均出来高(30日) > 100万株",
        "RSI(2) < 10  [日足]",
        "株価 > EMA(200)  ← 上昇トレンド",
        "EMA(200) ≤ EMA(150)  ← 中期上昇",
        "時価総額 > 20億 USD",
    ],
    "result_range":  [0, 150],
    "output_file":   "us_screening_{date}.csv",
}

JP_MARKET = {
    "name":         "日本株",
    "flag":         "🇯🇵",
    "url":          "https://scanner.tradingview.com/japan/scan",
    "schedule_hour":   15,
    "schedule_minute": 10,
    "cap_unit":        1_000_000_000,
    "cap_unit_label":  "億円",
    "filters": [
        {"left": "average_volume_30d_calc", "operation": "greater", "right": 1_000_000},
        {"left": "RSI2",                    "operation": "less",    "right": 10},
        {"left": "close",                   "operation": "eless",   "right": 3000},
        {"left": "EMA200",                  "operation": "less",    "right": "close"},
        {"left": "EMA200",                  "operation": "eless",   "right": "EMA150"},
        {"left": "market_cap_basic",        "operation": "greater", "right": 200_000_000_000},
    ],
    "criteria": [
        "平均出来高(30日) > 100万株",
        "RSI(2) < 10  [日足]",
        "株価 ≤ 3,000円",
        "株価 > EMA(200)  ← 上昇トレンド",
        "EMA(200) ≤ EMA(150)  ← 中期上昇",
        "時価総額 > 2,000億円",
    ],
    "result_range":  [0, 150],
    "output_file":   "jp_screening_{date}.csv",
}

MAX_POSITIONS = 3
MAX_DEPLOY_PCT = 0.60
EXIT_RSI_THRESHOLD = 70
MAX_HOLD_DAYS = 10
SWAP_SCORE_THRESHOLD = 20
SWAP_MIN_DAYS = 1
SWAP_NEAR_TARGET_PCT = 2.0
SWAP_MAX_LOSS_PCT = -5.0

US_MARKET["capital"]          = 2_000
US_MARKET["lot_size"]         = 1
US_MARKET["currency"]         = "USD"
US_MARKET["market_key"]       = "us"
US_MARKET["exchange_prefix"]  = ""

JP_MARKET["capital"]          = 3_200_000
JP_MARKET["lot_size"]         = 100
JP_MARKET["currency"]         = "JPY"
JP_MARKET["market_key"]       = "jp"
JP_MARKET["exchange_prefix"]  = "TSE:"

POLL_INTERVAL_SEC = 60

LINUX_SERVER_HOST = ""
LINUX_SERVER_USER = ""
LINUX_SERVER_KEY  = ""
LINUX_SERVER_DIR  = "~/trading"

OUTPUT_DIR = "."
