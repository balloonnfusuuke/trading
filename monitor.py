"""
monitor.py  ─  ポジション監視スクリプト
=============================================================
取引時間中に5分おきに実行し、損切り・利確ラインへの接触を
Discord に通知する。

【PC での実行（Task Scheduler で自動実行）】
  python monitor.py           # 全市場チェック
  python monitor.py --test    # Discord テスト送信

【Oracle Cloud VM での cron 設定例（UTC基準）】
  # 日本株 9:00-15:30 JST = 0:00-6:30 UTC
  */5 0-6 * * 1-5 cd /home/ubuntu/trading && python3 monitor.py

  # 米国株 22:30-翌5:00 JST = 13:30-翌20:00 UTC (EDT)
  */5 13-23 * * 1-5 cd /home/ubuntu/trading && python3 monitor.py
  */5 0-20  * * 2-6 cd /home/ubuntu/trading && python3 monitor.py
"""

import sys
import io
import csv
import json
import logging
import requests
from datetime import datetime, time as dtime
from pathlib import Path

# Windows コンソール UTF-8 対応
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config

# ── ロギング ──────────────────────────────────────────────────
_log_path = Path(config.OUTPUT_DIR) / "monitor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

POSITIONS_FILE = Path(config.OUTPUT_DIR) / "positions.json"
ALERTS_FILE    = Path(config.OUTPUT_DIR) / "alerts_sent.json"


# ── Discord 送信 ───────────────────────────────────────────────
def send_discord(message: str) -> bool:
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        logger.warning("DISCORD_WEBHOOK_URL が未設定です")
        return False
    try:
        r = requests.post(url, json={"content": message}, timeout=10)
        if r.status_code not in (200, 204):
            logger.warning(f"Discord 送信失敗: {r.status_code}")
            return False
        return True
    except Exception as e:
        logger.error(f"Discord 送信エラー: {e}")
        return False


# ── 取引時間チェック（JST 基準）────────────────────────────────
def is_market_open(market_key: str) -> bool:
    """現在が対象市場の取引時間内かどうかを JST で判定する。"""
    now = datetime.now()
    if now.weekday() >= 5:      # 土日はスキップ
        return False
    t = now.hour * 60 + now.minute
    if market_key == "jp":
        return 540 <= t <= 960          # 9:00-16:00 JST
    elif market_key == "us":
        return t >= 1350 or t <= 300    # 22:30-翌5:00 JST (EDT 基準)
    return False


# ── ポジション読み込み ─────────────────────────────────────────
def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"us": [], "jp": []}


def get_cumulative_pnl(market_key: str) -> tuple[float, int]:
    """
    pnl_log.csv から指定市場の通算確定損益と取引件数を返す（今日分も含む）。
    Returns: (total_pnl, trade_count)
    """
    pnl_file = Path(__file__).parent / "pnl_log.csv"
    if not pnl_file.exists():
        return 0.0, 0
    total = 0.0
    count = 0
    try:
        with open(pnl_file, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("market") != market_key:
                    continue
                try:
                    total += float(row["pnl"])
                    count += 1
                except (ValueError, KeyError):
                    pass
    except Exception:
        pass
    return total, count


# ── 送信済みアラート管理（重複通知防止）───────────────────────
def load_alerts() -> set:
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("alerted", []))
    return set()


def save_alerts(alerted: set):
    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"alerted": list(alerted)}, f, ensure_ascii=False, indent=2)


# ── TradingView API で価格取得 ────────────────────────────────
def _resolve_us_symbols(tickers: list, market: dict) -> dict:
    """
    exchange prefix が不明な US ティッカーを TradingView のフィルター検索で解決する。
    戻り値: {short_ticker: full_symbol}
    """
    if not tickers:
        return {}
    headers = {
        "User-Agent":   "Mozilla/5.0",
        "Content-Type": "application/json",
        "Referer":      "https://www.tradingview.com/",
        "Origin":       "https://www.tradingview.com",
        "Cookie":       f"sessionid={config.SESSION_ID}",
    }
    # TradingView のシンボル検索 API で各ティッカーを照会
    resolved = {}
    for tk in tickers:
        try:
            r = requests.get(
                f"https://symbol-search.tradingview.com/symbol_search/"
                f"?text={tk}&exchange=&type=stock&lang=en&domain=production",
                headers=headers, timeout=10
            )
            if r.status_code == 200:
                for item in r.json():
                    sym = item.get("symbol", "")
                    exch = item.get("exchange", "")
                    if sym.upper() == tk.upper() and exch:
                        resolved[tk] = f"{exch}:{sym}"
                        break
        except Exception:
            pass
    return resolved


def fetch_prices(tickers_or_pos: list, market: dict) -> dict:
    """
    指定ティッカーの現在値・高値・安値・RSI(2) を取得する。
    tickers_or_pos には ticker 文字列のリスト、または positions リストを渡せる。
    positions リストの場合、"symbol"（取引所プレフィックス付き）があればそれを使う。
    戻り値のキーは ticker（プレフィックスなし）。
    """
    if not tickers_or_pos:
        return {}

    prefix = market.get("exchange_prefix", "")

    # positions リストか文字列リストかを判別してシンボルマップを構築
    if isinstance(tickers_or_pos[0], dict):
        full_map = {}  # full_symbol -> short_ticker
        unresolved = []
        for p in tickers_or_pos:
            sym = p.get("symbol", "")
            tk  = p["ticker"]
            if sym and ":" in sym:
                full_map[sym] = tk
            elif prefix:
                full_map[f"{prefix}{tk}"] = tk
            else:
                unresolved.append(tk)
        # exchange 不明な US 銘柄を解決
        if unresolved:
            resolved = _resolve_us_symbols(unresolved, market)
            for tk in unresolved:
                if tk in resolved:
                    full_map[resolved[tk]] = tk
                else:
                    full_map[tk] = tk   # 最終手段：プレフィックスなしで試行
    else:
        full_map = {f"{prefix}{t}": t for t in tickers_or_pos}

    full = list(full_map.keys())
    payload = {
        "filter":  [],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": []}, "tickers": full},
        "columns": ["name", "close", "high", "low", "RSI2"],
        "range":   [0, len(full)],
    }
    headers = {
        "User-Agent":   "Mozilla/5.0",
        "Content-Type": "application/json",
        "Referer":      "https://www.tradingview.com/",
        "Origin":       "https://www.tradingview.com",
        "Cookie":       f"sessionid={config.SESSION_ID}",
    }
    try:
        r = requests.post(market["url"], headers=headers,
                          json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning(f"価格取得失敗: {r.status_code}")
            return {}
        result = {}
        for item in r.json().get("data", []):
            d = item.get("d", [])
            if len(d) >= 4:
                raw   = str(d[0])
                short = full_map.get(raw) or (raw.split(":")[-1] if ":" in raw else raw)
                result[short] = {
                    "close": d[1],
                    "high":  d[2],
                    "low":   d[3],
                    "rsi2":  d[4] if len(d) > 4 else None,
                }
        return result
    except Exception as e:
        logger.error(f"価格取得エラー: {e}")
        return {}


# ── 市場ごとのポジション監視 ───────────────────────────────────
def monitor_market(market: dict, force: bool = False):
    """
    オープンポジションを確認し、条件を満たしたら Discord に通知する。
    force=True の場合は取引時間外でも実行する（テスト用）。
    """
    market_key = market["market_key"]
    flag       = market["flag"]
    name       = market["name"]
    currency   = market.get("currency", "JPY")
    cur_sym    = "¥" if currency == "JPY" else "$"

    if not force and not is_market_open(market_key):
        logger.debug(f"[{name}] 取引時間外 → スキップ")
        return

    all_pos  = load_positions()
    open_pos = [p for p in all_pos.get(market_key, []) if p["status"] == "open"]

    if not open_pos:
        logger.info(f"[{name}] オープンポジションなし")
        return

    logger.info(f"[{name}] {len(open_pos)}件のポジションを確認中...")

    alerted = load_alerts()
    prices  = fetch_prices(open_pos, market)
    now_str = datetime.now().strftime("%m/%d %H:%M")

    for pos in open_pos:
        tk    = pos["ticker"]
        dat   = prices.get(tk)
        if not dat:
            logger.warning(f"  {tk}: 価格取得失敗")
            continue

        close = dat["close"]
        high  = dat["high"]
        low   = dat["low"]
        rsi2  = dat["rsi2"]
        entry = pos["entry_price"]

        alert_key = None
        msg       = None

        # ① 損切りライン到達（最優先）
        if low is not None and low <= pos["stop_loss"]:
            alert_key = f"{tk}_stop_{pos['entry_date']}"
            pnl = (pos["stop_loss"] - entry) * pos["shares"]
            msg = (
                f"🚨 **損切りライン到達** {flag} `{tk}`\n"
                f"🕐 {now_str}\n"
                f"▸ エントリー : {cur_sym}{entry:,.1f}\n"
                f"▸ 損切りライン: {cur_sym}{pos['stop_loss']:,.1f}\n"
                f"▸ 現在値     : {cur_sym}{close:,.1f}\n"
                f"▸ 想定損失   : {cur_sym}{pnl:,.0f}\n"
                f"⚡ **損切り注文を確認してください！**"
            )

        # ② RSI(2) 回復（売りシグナル）
        elif rsi2 is not None and rsi2 > config.EXIT_RSI_THRESHOLD:
            alert_key  = f"{tk}_rsi_{pos['entry_date']}"
            unrealized = (close - entry) * pos["shares"]
            msg = (
                f"📊 **RSI 回復シグナル** {flag} `{tk}`\n"
                f"🕐 {now_str}\n"
                f"▸ RSI(2)     : {rsi2:.1f}（>{config.EXIT_RSI_THRESHOLD} で売りシグナル）\n"
                f"▸ エントリー : {cur_sym}{entry:,.1f}\n"
                f"▸ 現在値     : {cur_sym}{close:,.1f}\n"
                f"▸ 評価損益   : {'+' if unrealized >= 0 else ''}{cur_sym}{unrealized:,.0f}\n"
                f"💡 **利確を検討してください！**"
            )

        # ③ 利確② 到達（R:R = 1:2）
        elif high is not None and high >= pos["target2"]:
            alert_key = f"{tk}_t2_{pos['entry_date']}"
            pnl = (pos["target2"] - entry) * pos["shares"]
            msg = (
                f"🎯 **利確② 到達** {flag} `{tk}` *(R:R = 1:2)*\n"
                f"🕐 {now_str}\n"
                f"▸ エントリー : {cur_sym}{entry:,.1f}\n"
                f"▸ 利確②ライン: {cur_sym}{pos['target2']:,.1f}\n"
                f"▸ 現在値     : {cur_sym}{close:,.1f}\n"
                f"▸ 期待利益   : +{cur_sym}{pnl:,.0f}\n"
                f"💰 **利確②を検討してください！**"
            )

        # ④ 利確① 到達（R:R = 1:1）
        elif high is not None and high >= pos["target1"]:
            alert_key = f"{tk}_t1_{pos['entry_date']}"
            pnl = (pos["target1"] - entry) * pos["shares"]
            msg = (
                f"✅ **利確① 到達** {flag} `{tk}` *(R:R = 1:1)*\n"
                f"🕐 {now_str}\n"
                f"▸ エントリー : {cur_sym}{entry:,.1f}\n"
                f"▸ 利確①ライン: {cur_sym}{pos['target1']:,.1f}\n"
                f"▸ 現在値     : {cur_sym}{close:,.1f}\n"
                f"▸ 期待利益   : +{cur_sym}{pnl:,.0f}\n"
                f"💰 **利確①を検討してください！**"
            )

        # 通知送信（重複防止：同じ alert_key は一度だけ送信）
        if alert_key and alert_key not in alerted:
            if send_discord(msg):
                alerted.add(alert_key)
                logger.info(f"  アラート送信: {tk} → {alert_key}")
        elif alert_key:
            logger.debug(f"  {tk}: 既送信済み ({alert_key})")

    save_alerts(alerted)


# ── デモ送信（全通知タイプ + 現在ポジション一覧）────────────────
def send_demo():
    """Discordに各通知タイプのサンプルと現在ポジションを送信する。"""
    now_str = datetime.now().strftime("%m/%d %H:%M")
    pos_all = load_positions()

    # ── ① 現在の保有ポジション一覧 ──────────────────────────────
    lines = [
        "📋 **【現在の保有ポジション一覧】**",
        f"🕐 {now_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    total_jp = 0
    total_us = 0

    for mkey, flag, cur_sym in [("jp","🇯🇵","¥"), ("us","🇺🇸","$")]:
        open_pos = [p for p in pos_all.get(mkey, []) if p["status"] == "open"]
        if not open_pos:
            continue
        lines.append(f"\n{flag} **{('日本株' if mkey=='jp' else '米国株')} {len(open_pos)}件**")
        for p in open_pos:
            invest = p["entry_price"] * p["shares"]
            lines += [
                f"▸ **{p['ticker']}**  エントリー:{cur_sym}{p['entry_price']:,.1f} × {p['shares']:,}株"
                f"  投資額:{cur_sym}{invest:,.0f}",
                f"   損切り:{cur_sym}{p['stop_loss']:,.1f}"
                f"  │  利確①:{cur_sym}{p['target1']:,.1f}"
                f"  │  利確②:{cur_sym}{p['target2']:,.1f}",
                f"   スコア:{p.get('score',0)}/100  エントリー日:{p['entry_date']}  {p.get('days_held',0)}日目",
            ]
            if mkey == "jp":
                total_jp += invest
            else:
                total_us += invest

    if total_jp > 0:
        lines.append(f"\n🇯🇵 日本株 合計投資額: ¥{total_jp:,.0f}")
    if total_us > 0:
        lines.append(f"🇺🇸 米国株 合計投資額: ${total_us:,.2f}")

    send_discord("\n".join(lines))

    import time
    time.sleep(1)

    # ── ② 通知タイプ サンプル（損切り）──────────────────────────
    send_discord(
        "⬇️ **以下は各アラートのサンプルです（実際の通知イメージ）**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    time.sleep(0.5)

    send_discord(
        "🚨 **損切りライン到達** 🇯🇵 `5471`\n"
        "🕐 04/02 10:15\n"
        "▸ エントリー : ¥1,812.5\n"
        "▸ 損切りライン: ¥1,680.0\n"
        "▸ 現在値     : ¥1,678.0\n"
        "▸ 想定損失   : ¥-39,750\n"
        "⚡ **損切り注文を確認してください！**"
    )
    time.sleep(0.5)

    send_discord(
        "✅ **利確① 到達** 🇯🇵 `7012` *(R:R = 1:1)*\n"
        "🕐 04/02 11:30\n"
        "▸ エントリー : ¥2,897.0\n"
        "▸ 利確①ライン: ¥3,156.2\n"
        "▸ 現在値     : ¥3,160.0\n"
        "▸ 期待利益   : +¥51,840\n"
        "💰 **利確①を検討してください！**"
    )
    time.sleep(0.5)

    send_discord(
        "🎯 **利確② 到達** 🇺🇸 `TRP` *(R:R = 1:2)*\n"
        "🕐 04/02 23:45\n"
        "▸ エントリー : $62.34\n"
        "▸ 利確②ライン: $65.90\n"
        "▸ 現在値     : $66.12\n"
        "▸ 期待利益   : +$21.36\n"
        "💰 **利確②を検討してください！**"
    )
    time.sleep(0.5)

    send_discord(
        "📊 **RSI 回復シグナル** 🇺🇸 `MPLX`\n"
        "🕐 04/03 02:10\n"
        "▸ RSI(2)     : 72.3（>70 で売りシグナル）\n"
        "▸ エントリー : $56.90\n"
        "▸ 現在値     : $59.20\n"
        "▸ 評価損益   : +$16.10\n"
        "💡 **利確を検討してください！**"
    )

    print("デモ送信完了")


# ── 時間ごとポジションレポート（日本株）──────────────────────────
def send_weekly_report():
    """
    先週（月〜金）の取引成績を集計して Discord に送信する。
    毎週月曜の朝に cron で実行することを想定。
    """
    from datetime import timedelta

    today     = datetime.now().date()
    # 先週月曜〜金曜の範囲を計算
    last_mon  = today - timedelta(days=today.weekday() + 7)
    last_fri  = last_mon + timedelta(days=4)
    range_str = f"{last_mon.strftime('%m/%d')}〜{last_fri.strftime('%m/%d')}"

    pnl_file = Path(__file__).parent / "pnl_log.csv"

    # 市場ごとに集計
    def aggregate(market_key: str) -> dict:
        trades = []
        if pnl_file.exists():
            try:
                with open(pnl_file, "r", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        if row.get("market") != market_key:
                            continue
                        try:
                            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                        except ValueError:
                            continue
                        if last_mon <= d <= last_fri:
                            trades.append({
                                "ticker":      row["ticker"],
                                "pnl":         float(row["pnl"]),
                                "pnl_pct":     float(row.get("pnl_pct", 0)),
                                "exit_reason": row.get("exit_reason", ""),
                                "days_held":   int(row.get("days_held", 0)),
                            })
            except Exception:
                pass

        if not trades:
            return None

        total_pnl  = sum(t["pnl"] for t in trades)
        wins       = [t for t in trades if t["pnl"] > 0]
        losses     = [t for t in trades if t["pnl"] <= 0]
        win_rate   = len(wins) / len(trades) * 100
        avg_win    = sum(t["pnl"] for t in wins)  / len(wins)  if wins   else 0
        avg_loss   = sum(t["pnl"] for t in losses)/ len(losses)if losses else 0
        pf         = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else float("inf")
        avg_days   = sum(t["days_held"] for t in trades) / len(trades)
        best       = max(trades, key=lambda t: t["pnl"])
        worst      = min(trades, key=lambda t: t["pnl"])

        return {
            "trades":    trades,
            "count":     len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "total_pnl": total_pnl,
            "win_rate":  win_rate,
            "avg_win":   avg_win,
            "avg_loss":  avg_loss,
            "pf":        pf,
            "avg_days":  avg_days,
            "best":      best,
            "worst":     worst,
        }

    jp = aggregate("jp")
    us = aggregate("us")

    if not jp and not us:
        send_discord(
            f"📅 **週次レポート** {range_str}\n"
            "先週は取引なし（または記録なし）"
        )
        logger.info("週次レポート: 先週取引なし")
        return

    lines = [
        f"📅 **週次パフォーマンスレポート**",
        f"📆 対象期間: {range_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for label, sym, data in [("🇯🇵 日本株", "¥", jp), ("🇺🇸 米国株", "$", us)]:
        if not data:
            lines += [f"{label}: 先週取引なし", ""]
            continue

        sign_t = "+" if data["total_pnl"] >= 0 else ""
        sign_w = "+" if data["avg_win"]   >= 0 else ""
        sign_l = "+" if data["avg_loss"]  >= 0 else ""
        pf_str = f"{data['pf']:.2f}" if data["pf"] != float("inf") else "∞"

        best_sign  = "+" if data["best"]["pnl"]  >= 0 else ""
        worst_sign = "+" if data["worst"]["pnl"] >= 0 else ""

        # 勝率バー（10段階）
        bar_filled = round(data["win_rate"] / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        lines += [
            f"**{label}**",
            f"  取引数: {data['count']}件  （{data['wins']}勝 {data['losses']}敗）",
            f"  勝率:  {data['win_rate']:.0f}%  [{bar}]",
            f"  週間損益:    {sign_t}{sym}{data['total_pnl']:,.0f}",
            f"  平均利益:    {sign_w}{sym}{data['avg_win']:,.0f}",
            f"  平均損失:    {sign_l}{sym}{data['avg_loss']:,.0f}",
            f"  PF:          {pf_str}",
            f"  平均保有日数: {data['avg_days']:.1f}日",
            f"  最良:  {data['best']['ticker']}  {best_sign}{sym}{data['best']['pnl']:,.0f} ({best_sign}{data['best']['pnl_pct']:.1f}%)",
            f"  最悪:  {data['worst']['ticker']}  {worst_sign}{sym}{data['worst']['pnl']:,.0f} ({worst_sign}{data['worst']['pnl_pct']:.1f}%)",
            "",
        ]

    # 取引明細
    for label, market_key, sym, data in [("🇯🇵 日本株", "jp", "¥", jp), ("🇺🇸 米国株", "us", "$", us)]:
        if not data:
            continue
        lines.append(f"**{label} 取引明細**")
        for t in sorted(data["trades"], key=lambda x: x["pnl"], reverse=True):
            icon  = "✅" if t["pnl"] > 0 else "❌"
            sign  = "+" if t["pnl"] >= 0 else ""
            lines.append(
                f"  {icon} {t['ticker']}  {sign}{sym}{t['pnl']:,.0f}"
                f" ({sign}{t['pnl_pct']:.1f}%)  {t['days_held']}日  {t['exit_reason']}"
            )
        lines.append("")

    send_discord("\n".join(lines))
    logger.info(f"週次レポート送信完了（JP:{jp['count'] if jp else 0}件 / US:{us['count'] if us else 0}件）")


def _build_position_report(market_cfg: dict, open_pos: list, force: bool = False) -> str:
    """
    指定市場のオープンポジション現況テキストを生成して返す。
    force=True の場合は取引時間外でも生成する。
    取引時間外かつ force=False の場合は空文字を返す。
    """
    market_key = market_cfg["market_key"]
    currency   = market_cfg.get("currency", "JPY")
    cur_sym    = "¥" if currency == "JPY" else "$"
    flag       = market_cfg.get("flag", "")
    name       = market_cfg.get("name", "")

    if not force and not is_market_open(market_key):
        return ""

    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")

    if not open_pos:
        return (
            f"📊 **{flag} {name} ポジション現況** 🕐 {now_str}\n"
            "保有中のポジションはありません。"
        )

    prices = fetch_prices(open_pos, market_cfg)

    total_unrealized = 0.0
    lines = [
        f"📊 **{flag} {name} ポジション現況** 🕐 {now_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for pos in open_pos:
        tk     = pos["ticker"]
        entry  = pos["entry_price"]
        shares = pos["shares"]
        dat    = prices.get(tk)

        if dat and dat["close"]:
            current  = dat["close"]
            diff     = current - entry
            diff_pct = diff / entry * 100
            unreal   = diff * shares
            total_unrealized += unreal

            if diff_pct >= 5:    icon = "🚀"
            elif diff_pct >= 2:  icon = "📈"
            elif diff_pct <= -4: icon = "🔴"
            elif diff_pct <= -2: icon = "📉"
            else:                icon = "➡️"

            to_t1 = (pos["target1"]   - current) / current * 100
            to_sl = (current - pos["stop_loss"]) / current * 100
            sign  = "+" if diff >= 0 else ""
            lines += [
                f"{icon} **{tk}**  {cur_sym}{entry:,.1f} → {cur_sym}{current:,.1f}"
                f"  {sign}{cur_sym}{diff:,.1f} ({sign}{diff_pct:.1f}%)",
                f"   {pos.get('days_held',0)}日目  "
                f"損切まで{to_sl:.1f}%  利確①まで{to_t1:.1f}%"
                f"  評価{sign}{cur_sym}{unreal:,.0f}",
            ]
        else:
            lines.append(f"➡️ **{tk}**  価格取得失敗")

    cum_pnl, trade_cnt = get_cumulative_pnl(market_key)
    total_pnl = total_unrealized + cum_pnl
    u_sign = "+" if total_unrealized >= 0 else ""
    c_sign = "+" if cum_pnl          >= 0 else ""
    t_sign = "+" if total_pnl        >= 0 else ""

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"含み損益（未決済）: **{u_sign}{cur_sym}{total_unrealized:,.0f}**",
        f"通算確定損益（{trade_cnt}件）: **{c_sign}{cur_sym}{cum_pnl:,.0f}**",
        f"➡ 合計損益: **{t_sign}{cur_sym}{total_pnl:,.0f}**",
    ]

    return "\n".join(lines)


def send_hourly_report():
    """
    日本株・米国株の保有ポジション現況を取引時間中に Discord へ送信する。
    """
    pos_all = load_positions()

    for market_cfg, mkey in [
        (config.JP_MARKET, "jp"),
        (config.US_MARKET, "us"),
    ]:
        open_pos = [p for p in pos_all.get(mkey, []) if p["status"] == "open"]
        text = _build_position_report(market_cfg, open_pos, force=False)
        if text:
            send_discord(text)
            logger.info(f"[{market_cfg['name']}] 時間レポート送信完了 ({len(open_pos)}件)")


# ── エントリーポイント ─────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--test" in args:
        ok = send_discord(
            "🔔 **ポジション監視システム 接続テスト**\n"
            f"🕐 {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}\n"
            "✅ 正常に動作しています！"
        )
        print("テスト通知 送信" + ("成功" if ok else "失敗"))

    elif "--demo" in args:
        send_demo()

    elif "--report" in args:
        # 日本株・米国株 両方を強制送信（取引時間外でも送る）
        logger.info("── 時間レポート送信 ──")
        pos_all = load_positions()
        sent = 0
        for market_cfg, mkey in [(config.JP_MARKET, "jp"), (config.US_MARKET, "us")]:
            open_pos = [p for p in pos_all.get(mkey, []) if p["status"] == "open"]
            text = _build_position_report(market_cfg, open_pos, force=True)
            if text:
                send_discord(text)
                sent += 1
        print(f"レポート送信完了（{sent}市場）")

    elif "--weekly" in args:
        logger.info("── 週次レポート送信 ──")
        send_weekly_report()
        print("週次レポート送信完了")

    elif "--force" in args:
        logger.info("── 強制チェックモード ──")
        monitor_market(config.US_MARKET, force=True)
        monitor_market(config.JP_MARKET, force=True)

    else:
        # 通常実行（取引時間内のみ動作）
        logger.info("── ポジション監視チェック開始 ──")
        monitor_market(config.US_MARKET)
        monitor_market(config.JP_MARKET)
        logger.info("── チェック完了 ──")
