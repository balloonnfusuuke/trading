"""
main.py  -  TradingView 自動スクリーニングシステム
============================================================
スケジュール:
  米国株 → 毎朝 4:40  に実行（US_MARKET）
  日本株 → 毎日 15:10 に実行（JP_MARKET）

使い方:
  python main.py              # 両市場スケジュール待機モード
  python main.py --us-now     # 米国株を今すぐ実行
  python main.py --jp-now     # 日本株を今すぐ実行
"""

import sys
import io
import csv
import time
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows コンソールでの UTF-8 エンコード問題を回避（絵文字・日本語を正しく表示）
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
import pandas as pd

import config

# ─── ロギング設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("screening.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─── TradingView API ───────────────────────────────────────────
def build_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Referer":  "https://www.tradingview.com/",
        "Origin":   "https://www.tradingview.com",
        "Cookie":   f"sessionid={config.SESSION_ID}",
    }


def fetch_screening_data(market: dict) -> pd.DataFrame:
    """
    指定マーケット設定で TradingView Scanner API を叩き DataFrame を返す。

    Parameters
    ----------
    market : dict
        config.US_MARKET または config.JP_MARKET

    Returns
    -------
    pd.DataFrame
        時価総額降順で並び替え済みのスクリーニング結果。
    """
    logger.info(f"[{market['name']}] TradingView API へリクエスト送信中...")

    payload = {
        "filter":  market["filters"],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": config.COLUMNS,
        "sort":    {"sortBy": "RSI[2]", "sortOrder": "asc"},
        "range":   market["result_range"],
    }

    try:
        response = requests.post(
            market["url"],
            headers=build_headers(),
            data=json.dumps(payload),
            timeout=30,
        )
    except requests.ConnectionError as e:
        logger.error(f"[{market['name']}] 接続エラー: {e}")
        raise

    # 認証エラー
    if response.status_code == 401:
        logger.error("=" * 55)
        logger.error(f"  [{market['name']}] 認証エラー（401 Unauthorized）")
        logger.error("  ⚠️  Cookie（SESSION_ID）の更新が必要です。")
        logger.error("  TradingView に再ログインし、config.py の SESSION_ID を更新するか、")
        logger.error("  GitHub Actions の場合は Secrets の TV_SESSION_ID を更新してください。")
        logger.error("=" * 55)
        sys.exit(1)

    if not response.ok:
        logger.error(f"[{market['name']}] HTTP エラー: {response.status_code} - {response.text[:300]}")
        response.raise_for_status()

    raw         = response.json()
    total_count = raw.get("totalCount", 0)
    data_list   = raw.get("data", [])

    logger.info(f"[{market['name']}] 取得件数: {len(data_list)} 件 / 全 {total_count} 件")

    if not data_list:
        logger.warning(f"[{market['name']}] スクリーニング結果が 0 件でした。")
        return pd.DataFrame(columns=list(config.COLUMN_LABELS.values()))

    # ─ DataFrame 構築 ────────────────────────────────────────
    rows = []
    for item in data_list:
        ticker_full  = item.get("s", "")   # 例: "NASDAQ:TRP" / "TSE:7203"
        ticker_short = ticker_full.split(":")[-1] if ":" in ticker_full else ticker_full
        values = item.get("d", [])

        row = {"name": ticker_short, "_symbol": ticker_full}
        # d[0] は "name" と同じ値なので index=1 から開始
        for i, col in enumerate(config.COLUMNS[1:], start=1):
            row[col] = values[i] if i < len(values) else None

        rows.append(row)

    df = pd.DataFrame(rows)
    df.rename(columns=config.COLUMN_LABELS, inplace=True)

    # 数値型変換
    for col in ["現在値", "RSI(2)", "SMA200", "EMA200", "EMA150", "平均出来高30日", "時価総額"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── 時価総額 降順に並び替え ──────────────────────────────
    df.sort_values("時価総額", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df


# ─── CSV 保存 ──────────────────────────────────────────────────
def save_to_csv(df: pd.DataFrame, market: dict) -> Path:
    date_str  = datetime.now().strftime("%Y%m%d_%H%M")
    filename  = market["output_file"].replace("{date}", date_str)
    save_path = Path(config.OUTPUT_DIR) / filename
    df.to_csv(save_path, index=False, encoding="utf-8-sig")
    logger.info(f"[{market['name']}] CSV 保存完了: {save_path.resolve()}")
    return save_path


# ─── エントリースコアリング ────────────────────────────────────
def calculate_entry_score(row: pd.Series) -> tuple:
    """
    複数指標からエントリースコア(0-100)と根拠リストを計算する。

    採点基準:
      RSI(2) 強度       ─ 最大 30pt  最も重要な売られすぎ指標
      EMA200 傾き       ─ 最大 25pt  上昇トレンドの持続性
      EMA200 乖離率     ─ 最大 15pt  EMAに近いほどリバウンド期待大
      EMA20 vs EMA50    ─ 最大 15pt  短中期トレンドの整合性
      ストキャスティクス ─ 最大 10pt  超短期の売られすぎ確認
      出来高サージ      ─ 最大  5pt  投げ売り集中のシグナル
    """
    score   = 0
    reasons = []

    rsi    = row.get("RSI(2)")
    close  = row.get("現在値")
    ema200 = row.get("EMA200")
    ema200p= row.get("EMA200前日")
    ema150 = row.get("EMA150")
    ema50  = row.get("EMA50")
    ema20  = row.get("EMA20")
    stoch  = row.get("Stoch.K")
    vol    = row.get("出来高")
    vol30  = row.get("平均出来高30日")

    # ① RSI(2) 強度 (0–30pt)
    if pd.notna(rsi):
        if rsi < 3:
            score += 30; reasons.append(f"RSI(2)={rsi:.1f} 極度売られすぎ +30")
        elif rsi < 6:
            score += 25; reasons.append(f"RSI(2)={rsi:.1f} 強い売られすぎ +25")
        else:
            score += 20; reasons.append(f"RSI(2)={rsi:.1f} 売られすぎ +20")

    # ② EMA200 傾き (0–25pt)  前日比で傾き計算
    if pd.notna(ema200) and pd.notna(ema200p) and ema200p > 0:
        slope = (ema200 - ema200p) / ema200p * 100
        if slope > 0.08:
            score += 25; reasons.append(f"EMA200傾き+{slope:.3f}%/日 強い上昇 +25")
        elif slope > 0.03:
            score += 20; reasons.append(f"EMA200傾き+{slope:.3f}%/日 上昇中 +20")
        elif slope > 0:
            score += 12; reasons.append(f"EMA200傾き+{slope:.3f}%/日 緩上昇 +12")
        elif slope > -0.03:
            score +=  5; reasons.append(f"EMA200傾き{slope:.3f}%/日 横ばい +5")
        else:
            reasons.append(f"EMA200傾き{slope:.3f}%/日 下降中 +0")
    else:
        slope = None

    # ③ EMA200 乖離率 – 近いほど良い (0–15pt)
    if pd.notna(close) and pd.notna(ema200) and ema200 > 0:
        dist = (close - ema200) / ema200 * 100
        if dist <= 5:
            score += 15; reasons.append(f"EMA200乖離{dist:.1f}% EMA近傍 +15")
        elif dist <= 12:
            score += 10; reasons.append(f"EMA200乖離{dist:.1f}% +10")
        elif dist <= 25:
            score +=  5; reasons.append(f"EMA200乖離{dist:.1f}% やや遠い +5")
        else:
            reasons.append(f"EMA200乖離{dist:.1f}% 乖離大 +0")
    else:
        dist = None

    # ④ EMA20 vs EMA50 短中期トレンド整合 (0–15pt)
    if pd.notna(ema20) and pd.notna(ema50) and ema50 > 0:
        gap = (ema20 - ema50) / ema50 * 100
        if gap > 0:
            score += 15; reasons.append(f"EMA20>EMA50(+{gap:.1f}%) 短期強い +15")
        elif gap > -3:
            score +=  8; reasons.append(f"EMA20≈EMA50({gap:.1f}%) 横ばい +8")
        else:
            reasons.append(f"EMA20<EMA50({gap:.1f}%) 短期弱い +0")

    # ⑤ ストキャスティクス (0–10pt)
    if pd.notna(stoch):
        if stoch < 15:
            score += 10; reasons.append(f"Stoch.K={stoch:.1f} 強い売られすぎ +10")
        elif stoch < 25:
            score +=  7; reasons.append(f"Stoch.K={stoch:.1f} 売られすぎ +7")
        elif stoch < 40:
            score +=  3; reasons.append(f"Stoch.K={stoch:.1f} やや低め +3")

    # ⑥ 出来高サージ – 投げ売り集中 (0–5pt)
    if pd.notna(vol) and pd.notna(vol30) and vol30 > 0:
        vr = vol / vol30
        if vr >= 2.0:
            score += 5; reasons.append(f"出来高{vr:.1f}倍 投げ売り集中 +5")
        elif vr >= 1.3:
            score += 3; reasons.append(f"出来高{vr:.1f}倍 増加 +3")

    return min(score, 100), reasons, slope, dist


# ─── エグジット（損切・利確）計算 ─────────────────────────────
def calculate_exit_targets(entry: float, atr, currency: str) -> dict:
    """
    ATR ベースの損切り・利確ラインと R:R を計算する。
    ATR が null の場合は entry の 4% をフォールバックとして使用。
    """
    if pd.notna(atr) and atr > 0:
        risk   = atr * 1.5        # 損切り幅 = ATR × 1.5
        method = f"ATR×1.5"
    else:
        risk   = entry * 0.04
        method = "4%固定"

    stop     = entry - risk
    target1  = entry + risk        # R:R 1:1
    target2  = entry + risk * 2    # R:R 1:2
    target3  = entry + risk * 3    # R:R 1:3

    return {
        "stop":       stop,
        "target1":    target1,
        "target2":    target2,
        "target3":    target3,
        "risk":       risk,
        "stop_pct":   -risk / entry * 100,
        "t1_pct":      risk / entry * 100,
        "t2_pct":      risk * 2 / entry * 100,
        "method":     method,
    }


# ─── 推奨エントリー計算 ────────────────────────────────────────
def _get_deployed_capital(market_key: str) -> float:
    """
    positions.json から指定市場のオープンポジションに投下済みの資金を返す。
    entry_price × shares の合計（ペーパートレード上の拘束額）。
    """
    try:
        pos_all   = load_positions()
        open_pos  = [p for p in pos_all.get(market_key, []) if p["status"] == "open"]
        return sum(p["entry_price"] * p["shares"] for p in open_pos)
    except Exception:
        return 0.0


def calculate_recommendations(df: pd.DataFrame, market: dict) -> list:
    """スコアリング→ポジションサイズ→エグジット計算を統合して返す。
    既存オープンポジションの拘束資金を差し引いた残余資本でサイズを計算する。
    """
    capital     = market.get("capital", 0)
    lot_size    = market.get("lot_size", 1)
    currency    = market.get("currency", "JPY")
    market_key  = market.get("market_key", "")
    max_pos     = config.MAX_POSITIONS

    # 既存ポジションで使用中の資金を差し引く
    deployed    = _get_deployed_capital(market_key)
    deploy_cap  = capital * config.MAX_DEPLOY_PCT
    available   = max(deploy_cap - deployed, 0)   # 残余運用枠

    # 残余枠を新規候補数（最大 MAX_POSITIONS 件）で割る
    per_pos_cap = available / max_pos if max_pos > 0 else 0

    logger.info(f"[{market.get('name','')}] 総資金:{capital:,.0f}  "
                f"運用枠:{deploy_cap:,.0f}  使用済:{deployed:,.0f}  "
                f"残余:{available:,.0f}  1枠:{per_pos_cap:,.0f}")

    # 全銘柄にスコアを付ける
    scored = []
    for _, row in df.dropna(subset=["RSI(2)", "現在値"]).iterrows():
        score, reasons, slope, dist = calculate_entry_score(row)
        scored.append((score, reasons, slope, dist, row))

    # スコア降順ソート → 上位 MAX_POSITIONS を選出
    scored.sort(key=lambda x: x[0], reverse=True)

    recs = []
    for rank, (score, reasons, slope, dist, row) in enumerate(scored[:max_pos], 1):
        price   = row["現在値"]
        atr     = row.get("ATR")
        if not price or price <= 0:
            continue

        # ポジションサイズ（残余資本ベース）
        if per_pos_cap <= 0:
            shares = lot_size   # 残余ゼロでも最低単元は表示
        else:
            shares = int(per_pos_cap / price / lot_size) * lot_size
            shares = max(shares, lot_size)

        invest  = shares * price
        pct     = invest / capital * 100

        # エグジット計算
        exits = calculate_exit_targets(price, atr, currency)

        recs.append({
            "rank":      rank,
            "ticker":    row.get("Ticker", ""),
            "symbol":    row.get("_symbol", row.get("Ticker", "")),  # 取引所プレフィックス付き
            "price":     price,
            "shares":    shares,
            "invest":    invest,
            "pct":       pct,
            "rsi":       row["RSI(2)"],
            "score":     score,
            "reasons":   reasons,
            "slope":     slope,
            "dist":      dist,
            "stoch":     row.get("Stoch.K"),
            "atr":       atr,
            "exits":     exits,
            "available": available,
            "deployed":  deployed,
        })

    return recs


# ─── ポジション管理（収支追跡）─────────────────────────────────
POSITIONS_FILE = Path(config.OUTPUT_DIR) / "positions.json"
PNL_LOG_FILE   = Path(config.OUTPUT_DIR) / "pnl_log.csv"


def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"us": [], "jp": []}


def save_positions(positions: dict):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def fetch_position_data(tickers: list, market: dict) -> dict:
    """オープンポジションの現在値・高値・安値・RSI2 を取得する。"""
    if not tickers:
        return {}

    prefix = market.get("exchange_prefix", "")
    full_tickers = [f"{prefix}{t}" for t in tickers]

    payload = {
        "filter":  [],
        "options": {"lang": "en"},
        "symbols": {"query": {"types": []}, "tickers": full_tickers},
        "columns": ["name", "close", "high", "low", "RSI2", "ATR"],
        "range":   [0, len(full_tickers)],
    }
    headers = {
        "User-Agent":    "Mozilla/5.0",
        "Content-Type":  "application/json",
        "Referer":       "https://www.tradingview.com/",
        "Origin":        "https://www.tradingview.com",
        "Cookie":        f"sessionid={config.SESSION_ID}",
    }
    try:
        r = requests.post(market["url"], headers=headers,
                          data=json.dumps(payload), timeout=15)
        if r.status_code != 200:
            logger.warning(f"ポジションデータ取得失敗: {r.status_code}")
            return {}
        result = {}
        for item in r.json().get("data", []):
            d = item.get("d", [])
            if len(d) >= 4:
                raw = str(d[0])
                # "TSE:7203" → "7203" に正規化
                tk = raw.split(":")[-1] if ":" in raw else raw
                result[tk] = {
                    "close": d[1],
                    "high":  d[2],
                    "low":   d[3],
                    "rsi2":  d[4] if len(d) > 4 else None,
                    "atr":   d[5] if len(d) > 5 else None,
                }
        return result
    except Exception as e:
        logger.error(f"ポジションデータ取得エラー: {e}")
        return {}


def _append_pnl_log(pos: dict, market_key: str):
    """クローズしたポジションを pnl_log.csv に追記する。"""
    fields = ["date", "market", "ticker", "entry_date", "entry_price",
              "exit_price", "shares", "pnl", "pnl_pct", "exit_reason",
              "days_held", "score"]
    file_exists = PNL_LOG_FILE.exists()
    with open(PNL_LOG_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            w.writeheader()
        w.writerow({
            "date":        datetime.now().strftime("%Y-%m-%d"),
            "market":      market_key,
            "ticker":      pos["ticker"],
            "entry_date":  pos["entry_date"],
            "entry_price": round(pos["entry_price"], 2),
            "exit_price":  round(pos["exit_price"],  2),
            "shares":      pos["shares"],
            "pnl":         round(pos["pnl"], 0),
            "pnl_pct":     round(pos.get("pnl_pct", 0), 2),
            "exit_reason": pos.get("exit_reason", ""),
            "days_held":   pos.get("days_held", 0),
            "score":       pos.get("score", 0),
        })


def _get_cumulative_pnl(market_key: str) -> float:
    """pnl_log.csv から市場ごとの累計損益を返す。"""
    if not PNL_LOG_FILE.exists():
        return 0.0
    total = 0.0
    with open(PNL_LOG_FILE, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("market") == market_key:
                try:
                    total += float(row["pnl"])
                except (ValueError, KeyError):
                    pass
    return total


def process_positions(market: dict) -> tuple:
    """
    オープンポジションに対して本日の値動きを確認し、
    損切り・利確・RSI出口・期間終了の判定を行う。

    Returns
    -------
    (closed_today, still_open, cum_pnl)
    """
    market_key = market["market_key"]
    all_pos    = load_positions()
    open_pos   = [p for p in all_pos.get(market_key, []) if p["status"] == "open"]

    if not open_pos:
        return [], [], _get_cumulative_pnl(market_key)

    tickers    = [p["ticker"] for p in open_pos]
    price_data = fetch_position_data(tickers, market)
    today      = datetime.now().strftime("%Y-%m-%d")

    closed_today = []
    still_open   = []

    for pos in open_pos:
        dat = price_data.get(pos["ticker"])
        if not dat:
            still_open.append(pos)
            continue

        close = dat["close"]
        high  = dat["high"]
        low   = dat["low"]
        rsi2  = dat["rsi2"]

        pos["current_price"]   = close
        pos["unrealized_pnl"]  = (close - pos["entry_price"]) * pos["shares"]
        pos["days_held"]       = pos.get("days_held", 0) + 1

        exit_price  = None
        exit_reason = None

        # 優先順位: 損切り > 期間終了 > RSI回復 > 利確②  > 利確①
        if low is not None and low <= pos["stop_loss"]:
            exit_price  = pos["stop_loss"]
            exit_reason = "損切り"
        elif pos["days_held"] >= config.MAX_HOLD_DAYS:
            exit_price  = close
            exit_reason = f"期間終了({config.MAX_HOLD_DAYS}日)"
        elif rsi2 is not None and rsi2 > config.EXIT_RSI_THRESHOLD:
            exit_price  = close
            exit_reason = f"RSI回復({rsi2:.1f})"
        elif high is not None and high >= pos["target2"]:
            exit_price  = pos["target2"]
            exit_reason = "利確②(R:R=1:2)"
        elif high is not None and high >= pos["target1"]:
            exit_price  = pos["target1"]
            exit_reason = "利確①(R:R=1:1)"

        if exit_price is not None:
            pnl = (exit_price - pos["entry_price"]) * pos["shares"]
            pos.update({
                "status":      "closed",
                "exit_date":   today,
                "exit_price":  exit_price,
                "exit_reason": exit_reason,
                "pnl":         pnl,
                "pnl_pct":     (exit_price - pos["entry_price"]) / pos["entry_price"] * 100,
            })
            closed_today.append(pos)
            _append_pnl_log(pos, market_key)
            logger.info(f"  [{pos['ticker']}] {exit_reason}  損益: {pnl:+,.0f}")
        else:
            still_open.append(pos)

    # JSON 更新（closedはそのまま残す、openは更新後リストに差し替え）
    all_pos[market_key] = (
        [p for p in all_pos.get(market_key, []) if p["status"] != "open"]
        + still_open
        + closed_today
    )
    save_positions(all_pos)

    return closed_today, still_open, _get_cumulative_pnl(market_key)


def add_new_positions(recs: list, market: dict):
    """推奨銘柄を新規オープンポジションとして登録する（重複スキップ）。
    金曜日は週末リスクのため新規エントリーをスキップする。
    """
    if datetime.now().weekday() == 4:   # 4 = 金曜日
        logger.info(f"  [金曜日] 週末リスク回避のため新規エントリーをスキップします。")
        return

    market_key = market["market_key"]
    all_pos    = load_positions()
    existing   = {p["ticker"] for p in all_pos.get(market_key, [])
                  if p["status"] == "open"}
    today      = datetime.now().strftime("%Y-%m-%d")

    added = []
    for r in recs:
        if r["ticker"] in existing:
            continue
        e = r["exits"]
        added.append({
            "ticker":        r["ticker"],
            "symbol":        r.get("symbol", r["ticker"]),  # 取引所プレフィックス付き
            "entry_date":    today,
            "entry_price":   r["price"],
            "shares":        r["shares"],
            "invest":        r["invest"],
            "stop_loss":     e["stop"],
            "target1":       e["target1"],
            "target2":       e["target2"],
            "target3":       e["target3"],
            "score":         r["score"],
            "status":        "open",
            "days_held":     0,
            "current_price": r["price"],
            "unrealized_pnl": 0.0,
        })

    all_pos[market_key] = all_pos.get(market_key, []) + added
    save_positions(all_pos)
    if added:
        logger.info(f"  新規ポジション登録: {[a['ticker'] for a in added]}")


def _calc_holding_value_score(pos: dict, current_price: float) -> tuple[float, str]:
    """
    既存ポジションを「今から継続して保有する価値」のスコアで再評価する。
    Returns: (holding_score, reason_str)
    """
    entry       = pos["entry_price"]
    days_held   = pos.get("days_held", 0)
    orig_score  = pos.get("score", 50)
    target1     = pos.get("target1", entry * 1.05)
    stop_loss   = pos.get("stop_loss", entry * 0.97)

    unreal_pct  = (current_price - entry) / entry * 100 if entry else 0
    to_t1_pct   = (target1 - current_price) / current_price * 100 if current_price else 0

    score  = float(orig_score)
    reason = []

    # 日数経過ペナルティ（1日 -5点）
    day_penalty = days_held * 5
    score -= day_penalty
    if day_penalty > 0:
        reason.append(f"保有{days_held}日(-{day_penalty}pt)")

    # 含み損ペナルティ
    if unreal_pct < -3:
        score -= 15
        reason.append(f"含み損{unreal_pct:.1f}%(-15pt)")
    elif unreal_pct < -1:
        score -= 7
        reason.append(f"含み損{unreal_pct:.1f}%(-7pt)")

    # 含み益ボーナス（勝ちトレードは引っ張る）
    if unreal_pct >= 5:
        score += 10
        reason.append(f"含み益{unreal_pct:.1f}%(+10pt)")
    elif unreal_pct >= 3:
        score += 5
        reason.append(f"含み益{unreal_pct:.1f}%(+5pt)")

    # 利確①に近い場合はボーナス（もう少しで利確）
    if 0 < to_t1_pct <= config.SWAP_NEAR_TARGET_PCT:
        score += 20
        reason.append(f"利確①まで{to_t1_pct:.1f}%(+20pt)")

    return score, " / ".join(reason) if reason else "変動なし"


def analyze_swap_candidates(
    open_pos: list,
    new_recs: list,
    price_data: dict,
    market: dict,
) -> list:
    """
    既存ポジションと新規推奨を比較し、入れ替え推奨リストを返す。

    Returns
    -------
    list of dict:
        sell_ticker, sell_score, sell_reason,
        buy_ticker, buy_score, score_diff,
        sell_pnl, sell_pnl_pct
    """
    swaps = []

    # 新候補のうち、すでに保有中でないものだけ対象
    held_tickers = {p["ticker"] for p in open_pos}
    candidates   = [r for r in new_recs if r["ticker"] not in held_tickers]

    if not candidates or not open_pos:
        return []

    for pos in open_pos:
        tk    = pos["ticker"]
        dat   = price_data.get(tk)
        if not dat:
            continue

        cur_price    = dat["close"]
        days_held    = pos.get("days_held", 0)
        unreal_pct   = (cur_price - pos["entry_price"]) / pos["entry_price"] * 100
        to_t1_pct    = (pos.get("target1", cur_price) - cur_price) / cur_price * 100

        # 最低保有日数未満はスキップ
        if days_held < config.SWAP_MIN_DAYS:
            continue

        # 利確ラインが近い場合はスキップ
        if 0 < to_t1_pct <= config.SWAP_NEAR_TARGET_PCT:
            continue

        # 深い含み損（損切り任せ）はスキップ
        if unreal_pct <= config.SWAP_MAX_LOSS_PCT:
            continue

        hold_score, hold_reason = _calc_holding_value_score(pos, cur_price)

        # 最も有望な新候補と比較
        best_candidate = max(candidates, key=lambda r: r["score"])
        score_diff = best_candidate["score"] - hold_score

        if score_diff >= config.SWAP_SCORE_THRESHOLD:
            pnl     = (cur_price - pos["entry_price"]) * pos["shares"]
            pnl_pct = unreal_pct
            swaps.append({
                "sell_ticker":  tk,
                "sell_score":   hold_score,
                "sell_reason":  hold_reason,
                "buy_ticker":   best_candidate["ticker"],
                "buy_score":    best_candidate["score"],
                "score_diff":   score_diff,
                "sell_pnl":     pnl,
                "sell_pnl_pct": pnl_pct,
                "buy_rec":      best_candidate,
            })

    # スコア差が大きい順にソート
    swaps.sort(key=lambda x: x["score_diff"], reverse=True)
    return swaps


def execute_swaps(swaps: list, market: dict) -> list:
    """
    入れ替え推奨を実行する。
    売却側ポジションをクローズし、新規推奨を登録する。
    金曜日は週末リスクのため新規買いが発生するスワップを実行しない。
    Returns: 実行したswapリスト
    """
    if not swaps:
        return []

    if datetime.now().weekday() == 4:   # 4 = 金曜日
        logger.info(f"  [金曜日] 週末リスク回避のため入れ替えをスキップします。")
        return []

    market_key = market["market_key"]
    all_pos    = load_positions()
    today      = datetime.now().strftime("%Y-%m-%d")
    executed   = []

    # 既に入れ替え対象として選ばれた buy_ticker の重複排除
    used_buy = set()

    for swap in swaps:
        if swap["buy_ticker"] in used_buy:
            continue

        # 売却側ポジションをクローズ
        for pos in all_pos.get(market_key, []):
            if pos["ticker"] == swap["sell_ticker"] and pos["status"] == "open":
                exit_price = pos.get("current_price", pos["entry_price"])
                pnl        = (exit_price - pos["entry_price"]) * pos["shares"]
                pnl_pct    = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100

                pos["status"]       = "closed"
                pos["exit_price"]   = exit_price
                pos["exit_date"]    = today
                pos["exit_reason"]  = f"入れ替え→{swap['buy_ticker']}"
                pos["pnl"]         = pnl
                pos["pnl_pct"]     = pnl_pct
                _append_pnl_log(pos, market_key)
                logger.info(f"  [入れ替え売却] {swap['sell_ticker']} → {exit_price:.2f} "
                            f"PnL:{'+' if pnl >= 0 else ''}{pnl:,.0f}")
                break

        # 新規ポジション登録
        rec = swap["buy_rec"]
        e   = rec["exits"]
        all_pos[market_key].append({
            "ticker":         rec["ticker"],
            "symbol":         rec.get("symbol", rec["ticker"]),
            "entry_date":     today,
            "entry_price":    rec["price"],
            "shares":         rec["shares"],
            "invest":         rec["invest"],
            "stop_loss":      e["stop"],
            "target1":        e["target1"],
            "target2":        e["target2"],
            "target3":        e["target3"],
            "score":          rec["score"],
            "status":         "open",
            "days_held":      0,
            "current_price":  rec["price"],
            "unrealized_pnl": 0.0,
        })
        logger.info(f"  [入れ替え購入] {rec['ticker']} @ {rec['price']:.2f}")
        used_buy.add(swap["buy_ticker"])
        executed.append(swap)

    save_positions(all_pos)
    return executed


def format_swap_section(swaps: list, executed: list, market: dict) -> list:
    """入れ替え推奨セクションのテキスト行を返す。"""
    if not swaps:
        return []

    currency = market.get("currency", "JPY")
    cur_sym  = "¥" if currency == "JPY" else "$"
    lines    = ["", "━━ 🔄 ポジション入れ替え推奨 ━━"]
    ex_tickers = {s["sell_ticker"] for s in executed}

    for swap in swaps:
        executed_mark = "✅ 実行済" if swap["sell_ticker"] in ex_tickers else "💡 推奨"
        pnl_sign = "+" if swap["sell_pnl"] >= 0 else ""
        lines += [
            f"{executed_mark}  【売】{swap['sell_ticker']} (継続価値:{swap['sell_score']:.0f}pt)",
            f"       {swap['sell_reason']}",
            f"       現在損益: {pnl_sign}{cur_sym}{swap['sell_pnl']:,.0f} ({pnl_sign}{swap['sell_pnl_pct']:.1f}%)",
            f"  →  【買】{swap['buy_ticker']} (スコア:{swap['buy_score']}/100  差:{swap['score_diff']:+.0f}pt)",
            "",
        ]
    return lines


def format_pnl_section(closed_today: list, still_open: list,
                        cum_pnl: float, market: dict) -> list:
    """収支サマリーの LINE テキスト行リストを返す。"""
    currency = market.get("currency", "JPY")
    def fm(v): return f"¥{v:,.0f}" if currency == "JPY" else f"${v:,.0f}"
    cur_sym  = "¥" if currency == "JPY" else "$"

    lines = ["", "━━ 本日の収支 ━━"]

    # ── クローズ分 ──────────────────────────────────────────────
    if closed_today:
        daily_pnl = sum(p["pnl"] for p in closed_today)
        sign      = "+" if daily_pnl >= 0 else ""
        lines.append(f"【クローズ {len(closed_today)}件】本日損益:{sign}{fm(daily_pnl)}")
        for p in closed_today:
            icon = "✅" if p["pnl"] >= 0 else "❌"
            sign = "+" if p["pnl"] >= 0 else ""
            lines += [
                f"  {icon}{p['ticker']}  {p['exit_reason']}",
                f"     {cur_sym}{p['entry_price']:,.1f}→{cur_sym}{p['exit_price']:,.1f}"
                f"  {sign}{fm(p['pnl'])}({p.get('pnl_pct',0):+.1f}%)",
            ]
    else:
        lines.append("【本日クローズ: なし】")

    # ── 保有中 ──────────────────────────────────────────────────
    if still_open:
        total_unreal = sum(p.get("unrealized_pnl", 0) for p in still_open)
        sign = "+" if total_unreal >= 0 else ""
        lines.append(f"【保有中 {len(still_open)}件】評価損益:{sign}{fm(total_unreal)}")
        for p in still_open:
            unreal = p.get("unrealized_pnl", 0)
            icon   = "📈" if unreal >= 0 else "📉"
            lines.append(
                f"  {icon}{p['ticker']}  {p.get('days_held',0)}日目  "
                f"評価{'+' if unreal>=0 else ''}{fm(unreal)}"
            )

    # ── 累計（pnl_log.csv の確定済みトレード合計）────────────────
    sign = "+" if cum_pnl >= 0 else ""
    lines.append(f"【累計損益（確定のみ）】{sign}{fm(cum_pnl)}")

    # ── 合計（確定 + いまの含み益）monitor の時間レポートに揃える ─
    unreal_total = sum(p.get("unrealized_pnl", 0) for p in still_open)
    if still_open:
        grand = cum_pnl + unreal_total
        g_sign = "+" if grand >= 0 else ""
        lines.append(f"【合計損益（確定＋含み）】{g_sign}{fm(grand)}")

    return lines


def format_positions_detail_section(
    still_open: list, price_data: dict, market: dict
) -> list:
    """
    保有中ポジションの現在値・損切・利確ライン・RSI を列挙する（スクリーニング 0 件時の本文用）。
    """
    if not still_open:
        return []

    currency = market.get("currency", "JPY")
    cur_sym = "¥" if currency == "JPY" else "$"

    def fm(v):
        return f"{cur_sym}{v:,.1f}"

    lines: list = ["", "━━ 保有ポジション（現在値・損切・利確）━━"]
    for p in still_open:
        tk = p["ticker"]
        dat = price_data.get(tk) or {}
        close_px = dat.get("close")
        if close_px is None:
            close_px = p.get("current_price", p.get("entry_price"))
        entry = float(p["entry_price"])
        shares = int(p["shares"])
        unreal = (close_px - entry) * shares if close_px is not None else p.get(
            "unrealized_pnl", 0
        )
        pct = (close_px - entry) / entry * 100 if close_px is not None and entry else 0.0
        rsi2 = dat.get("rsi2")
        sl = p.get("stop_loss")
        t1, t2, t3 = p.get("target1"), p.get("target2"), p.get("target3")
        days = p.get("days_held", 0)

        lines.append(f"  【{tk}】 {days}日目  登録スコア:{p.get('score', '-')}")
        u_sign = "+" if unreal >= 0 else ""
        lines.append(
            f"    取得{fm(entry)} → 現在{fm(close_px)} ({pct:+.1f}%)  "
            f"評価{u_sign}{cur_sym}{abs(unreal):,.0f}"
        )
        lines.append(
            f"    損切{fm(sl)}  利確①{fm(t1)}  利確②{fm(t2)}  利確③{fm(t3)}"
        )
        if rsi2 is not None:
            lines.append(
                f"    RSI(2):{rsi2:.2f}  （出口目安 RSI>{config.EXIT_RSI_THRESHOLD}）"
            )
        lines.append("")

    return lines


# ─── LINE 通知 ─────────────────────────────────────────────────
def format_line_message(df: pd.DataFrame, market: dict,
                        recs: list = None,
                        closed_today: list = None,
                        still_open: list = None,
                        cum_pnl: float = 0.0,
                        swaps: list = None,
                        executed_swaps: list = None,
                        position_price_data: Optional[dict] = None) -> str:
    """スコアリング付き分析レポートを Discord 送信用テキストにフォーマットする。"""
    now      = datetime.now().strftime("%Y/%m/%d %H:%M")
    unit     = market["cap_unit"]
    label    = market["cap_unit_label"]
    flag     = market["flag"]
    name     = market["name"]
    criteria = market.get("criteria", [])
    capital  = market.get("capital", 0)
    currency = market.get("currency", "JPY")
    cur_sym  = "¥" if currency == "JPY" else "$"
    deploy   = capital * config.MAX_DEPLOY_PCT
    reserve  = capital - deploy

    # recs が渡されなかった場合は内部計算（後方互換）
    if recs is None:
        recs = calculate_recommendations(df, market)

    def fm(v): return f"¥{v:,.0f}" if currency == "JPY" else f"${v:,.0f}"

    lines = [
        f"{flag} {name} Connors RSI(2)戦略",
        f"📅 {now}",
    ]
    if df.empty:
        lines += ["⚠️ スクリーニング該当: 0 件（収支・保有の状況は以下）", ""]
    else:
        lines.append("")

    lines.append("【スクリーニング条件】")
    for c in criteria:
        lines.append(f"  ✔ {c}")

    # ── 収支サマリー（PnL）──────────────────────────────────────
    # closed/still が None でも累計・本日収支ブロックは必ず出す（None は空リスト扱い）
    lines += format_pnl_section(
        closed_today if closed_today is not None else [],
        still_open if still_open is not None else [],
        float(cum_pnl) if cum_pnl is not None else 0.0,
        market,
    )

    # 該当銘柄なしでも、保有があれば株価・利確を明示
    if df.empty and still_open:
        lines += format_positions_detail_section(
            still_open, position_price_data or {}, market
        )

    # ── ポジション入れ替え推奨 ────────────────────────────────
    if swaps:
        lines += format_swap_section(swaps, executed_swaps or [], market)

    # ── 推奨エントリー分析 ────────────────────────────────────
    deployed  = recs[0].get("deployed",  0) if recs else 0
    available = recs[0].get("available", deploy - deployed) if recs else deploy
    lines += [
        "",
        f"━━ 推奨エントリー TOP{config.MAX_POSITIONS} ━━",
        f"総資金:{fm(capital)}  運用枠:{fm(deploy)}({int(config.MAX_DEPLOY_PCT*100)}%)",
        f"使用中:{fm(deployed)}  残余枠:{fm(available)}",
        f"手元保留:{fm(reserve)}({int((1-config.MAX_DEPLOY_PCT)*100)}%) ← キープ",
        "",
    ]

    is_friday = datetime.now().weekday() == 4
    if not recs:
        lines.append("⚠️ 本日の推奨銘柄なし")
    elif is_friday:
        lines.append("🚫 本日は金曜日のため新規エントリーをスキップ（週末リスク回避）")
        lines.append("   ※ 参考情報として候補銘柄を表示します")
        lines.append("")
        total_invest = sum(r["invest"] for r in recs)
        for r in recs:
            e = r["exits"]
            lines += [
                f"  📋 【{r['ticker']}】 スコア:{r['score']}/100  RSI(2):{r['rsi']:.2f}",
                f"     {cur_sym}{r['price']:,.1f}  損切:{cur_sym}{e['stop']:,.1f}  利確①:{cur_sym}{e['target1']:,.1f}",
            ]
    else:
        total_invest = sum(r["invest"] for r in recs)
        for r in recs:
            e = r["exits"]
            lines += [
                f"{'★'*r['rank']} {r['rank']}位 【{r['ticker']}】 スコア:{r['score']}/100",
                f"  現在値:{cur_sym}{r['price']:,.1f}  RSI(2):{r['rsi']:.2f}",
            ]
            if r["slope"] is not None:
                arrow = "↗" if r["slope"] > 0 else ("↘" if r["slope"] < -0.03 else "→")
                lines.append(f"  EMA200傾き:{r['slope']:+.3f}%/日{arrow}  EMA乖離:{r['dist']:+.1f}%")
            if r["stoch"] is not None:
                lines.append(f"  Stoch.K:{r['stoch']:.1f}  ATR:{r['atr']:.1f}")

            lines += [
                f"  ─ エントリー ─",
                f"  購入:{r['shares']:,}株  投資額:{fm(r['invest'])}({r['pct']:.1f}%)",
                f"  ─ リスク管理({e['method']}) ─",
                f"  損切り:{cur_sym}{e['stop']:,.1f} ({e['stop_pct']:+.1f}%)",
                f"  利確①:{cur_sym}{e['target1']:,.1f} ({e['t1_pct']:+.1f}%)  R:R=1:1",
                f"  利確②:{cur_sym}{e['target2']:,.1f} ({e['t2_pct']:+.1f}%)  R:R=1:2",
                f"  最大損失:{fm(r['shares'] * e['risk'])}",
                "",
            ]

        lines.append(f"合計投資額:{fm(total_invest)}")

    # ── 全該当銘柄一覧 ────────────────────────────────────────
    lines += [
        "",
        f"━━ 全該当銘柄 {len(df)}件（時価総額降順）━━",
    ]
    if df.empty:
        lines.append("該当なし")
    else:
        rec_tickers = {r["ticker"] for r in recs}
        for _, row in df.iterrows():
            ticker  = row.get("Ticker", "")
            close   = row.get("現在値", "")
            rsi     = row.get("RSI(2)")
            cap     = row.get("時価総額")
            cap_str = f"{cap/unit:.0f}{label}" if pd.notna(cap) and cap else "-"
            rsi_str = f"{rsi:.1f}" if pd.notna(rsi) else "-"
            star    = "★" if ticker in rec_tickers else "  "
            lines.append(f"{star}{ticker:<7}RSI:{rsi_str} {cur_sym}{close} {cap_str}")

    return "\n".join(lines)


def sync_positions_to_linux():
    """
    positions.json を Linux サーバーに SCP で転送する。
    スクリーニング完了後に呼び出し、監視スクリプトが最新データを参照できるようにする。
    """
    import os
    import subprocess

    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("SKIP_SCP_SYNC", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return

    host = getattr(config, "LINUX_SERVER_HOST", "")
    user = getattr(config, "LINUX_SERVER_USER", "")
    key  = getattr(config, "LINUX_SERVER_KEY",  "")
    dst  = getattr(config, "LINUX_SERVER_DIR",  "~/trading")

    if not host or not user:
        return   # 設定なければスキップ

    files_to_sync = [
        (str(POSITIONS_FILE), "positions.json"),
        (str(PNL_LOG_FILE),   "pnl_log.csv"),
    ]
    for src, fname in files_to_sync:
        if not Path(src).exists():
            continue
        cmd = ["scp", "-i", key, "-o", "StrictHostKeyChecking=no",
               src, f"{user}@{host}:{dst}/{fname}"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0:
                logger.info(f"  {fname} → Linux サーバー 同期完了")
            else:
                logger.warning(f"  Linux 同期失敗 ({fname}): {result.stderr.decode(errors='replace')[:100]}")
        except Exception as e:
            logger.warning(f"  Linux 同期エラー ({fname}): {e}")


def send_discord_message(text: str, market_name: str) -> bool:
    """
    Discord Incoming Webhook でスクリーニングレポートを送信する。
    2000文字を超える場合は複数回に分割して送信する。
    """
    url = (config.DISCORD_WEBHOOK_URL or "").strip()
    if not url:
        logger.warning(f"[{market_name}] DISCORD_WEBHOOK_URL が未設定です。")
        return False

    # 診断用（トークンはログに出さない）
    if "/webhooks/" in url:
        _pre, _, _rest = url.partition("/webhooks/")
        _wid = _rest.split("/")[0] if _rest else "?"
        logger.info(
            f"[{market_name}] Discord 送信: host={_pre.split('//')[-1]} "
            f"webhook_id={_wid} url_len={len(url)}"
        )
    else:
        logger.warning(f"[{market_name}] Discord URL に /webhooks/ がありません（貼り間違いの可能性）")

    # 行単位で 1900 文字以内に分割（Discord の 2000 文字制限に対応）
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        # +5 はコードブロック記号（``` \n ... \n ```）の分
        if current_len + len(line) + 1 > 1900:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))

    try:
        for chunk in chunks:
            payload = {"content": f"```\n{chunk}\n```"}
            res = requests.post(url, json=payload, timeout=15)
            logger.info(f"[{market_name}] Discord HTTP 応答: {res.status_code}")
            if res.status_code not in (200, 204):
                logger.error(f"[{market_name}] Discord 送信失敗: {res.status_code} {res.text[:200]}")
                return False
        logger.info(f"[{market_name}] Discord 通知 送信完了 ({len(chunks)}件)")
        return True
    except Exception as e:
        logger.error(f"[{market_name}] Discord 送信エラー: {e}")
        return False


# ─── メイン処理（1マーケット分）─────────────────────────────────
def run(market: dict):
    """スクリーニング・ポジション管理・LINE通知を一括実行する。"""
    flag = market["flag"]
    name = market["name"]

    logger.info("━" * 55)
    logger.info(f"  {flag} {name} スクリーニング開始")
    logger.info(f"  実行時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("━" * 55)

    # ① オープンポジションのクローズ判定（前日エントリー分）
    logger.info(f"[{name}] ポジション確認中...")
    closed_today, still_open, cum_pnl = process_positions(market)
    logger.info(f"  本日クローズ:{len(closed_today)}件  保有中:{len(still_open)}件  "
                f"累計損益:{'+' if cum_pnl>=0 else ''}{cum_pnl:,.0f}")

    # ② スクリーニング
    df = fetch_screening_data(market)

    # ─ コンソール表示 ─────────────────────────────────────────
    pd.set_option("display.max_rows",    50)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width",       140)
    pd.set_option("display.float_format", "{:.2f}".format)
    print(f"\n{'─'*60}")
    print(f" {flag} {name}  スクリーニング結果（時価総額降順）")
    print(f"{'─'*60}")
    if df.empty:
        print(" ⚠️  本日の該当銘柄なし")
    else:
        display_cols = ["Ticker", "現在値", "RSI(2)", "EMA200", "EMA150", "時価総額"]
        print(df[[c for c in display_cols if c in df.columns]].to_string(index=False))
    print()

    if df.empty:
        logger.warning(
            f"[{name}] スクリーニング該当 0 件。CSV はスキップし、収支・保有の Discord レポートを送ります。"
        )
        price_for_msg: dict = {}
        if still_open:
            price_for_msg = fetch_position_data(
                [p["ticker"] for p in still_open], market
            )
        msg = format_line_message(
            df,
            market,
            recs=[],
            closed_today=closed_today,
            still_open=still_open,
            cum_pnl=cum_pnl,
            swaps=[],
            executed_swaps=[],
            position_price_data=price_for_msg,
        )
        if not send_discord_message(msg, name):
            logger.error(f"[{name}] Discord 送信失敗のため異常終了します。")
            sys.exit(1)
        mark_market_ran_today(market["market_key"])
        return

    # ③ 推奨銘柄計算
    recs = calculate_recommendations(df, market)

    # ③-b ポジション入れ替え分析
    all_pos_now  = load_positions()
    open_pos_now = [p for p in all_pos_now.get(market["market_key"], [])
                    if p["status"] == "open"]
    price_data_now = fetch_position_data(
        [p["ticker"] for p in open_pos_now], market
    ) if open_pos_now else {}

    swaps    = analyze_swap_candidates(open_pos_now, recs, price_data_now, market)
    executed = execute_swaps(swaps, market)

    # ④ CSV 保存
    save_path = save_to_csv(df, market)

    # ⑤ Discord 通知（収支 + 入れ替え + 推奨エントリー + 全銘柄一覧）
    msg = format_line_message(df, market,
                              recs=recs,
                              closed_today=closed_today,
                              still_open=still_open,
                              cum_pnl=cum_pnl,
                              swaps=swaps,
                              executed_swaps=executed)
    if not send_discord_message(msg, name):
        logger.error(f"[{name}] Discord 送信失敗のため異常終了します。")
        sys.exit(1)

    # ⑥ 新規推奨をポジションに登録（Discord送信後・入れ替えで登録済みを除く）
    add_new_positions(recs, market)

    # ⑦ positions.json を Linux サーバーに同期
    sync_positions_to_linux()

    mark_market_ran_today(market["market_key"])

    logger.info("━" * 55)
    logger.info(f"  完了。{len(df)} 銘柄 / 保存先: {save_path.name}")
    logger.info("━" * 55)


# ─── スケジューラー状態（スリープ復帰・電源投入後も当日分を実行する）────
SCHEDULER_STATE_FILE = Path(__file__).parent / "scheduler_state.json"


def load_scheduler_state() -> dict:
    if not SCHEDULER_STATE_FILE.exists():
        return {}
    try:
        with open(SCHEDULER_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def mark_market_ran_today(market_key: str) -> None:
    """そのマーケットの本日分スクリーニングを完了済みとして記録する。"""
    state = load_scheduler_state()
    state[market_key] = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(SCHEDULER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"scheduler_state 保存失敗: {e}")


def should_run_scheduled(market: dict, now: datetime) -> bool:
    """
    本日まだ未実行で、かつ今日の実行時刻（時:分）を過ぎていれば True。
    「15:10ちょうどに起きていない」と取り逃しても、当日中なら再試行できる。
    """
    state = load_scheduler_state()
    mk = market["market_key"]
    today = now.strftime("%Y-%m-%d")
    if state.get(mk) == today:
        return False
    h = market["schedule_hour"]
    m = market["schedule_minute"]
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return now >= target


# ─── スケジューラー（1マーケット・別スレッドで動作）──────────────
def market_scheduler(market: dict):
    """
    指定マーケットの設定時刻を過ぎたら当日1回実行する。
    スリープ・電源オフで「その分」を跨いでも、当日中に復帰すれば実行される。
    threading.Thread で起動すること。
    """
    h = market["schedule_hour"]
    m = market["schedule_minute"]
    logger.info(
        f"[{market['name']}] スケジュール登録: 毎日 {h:02d}:{m:02d} 以降に1回実行（取り逃し時は当日中に追い実行）"
    )
    while True:
        now = datetime.now()
        if should_run_scheduled(market, now):
            try:
                run(market)
            except Exception as e:
                logger.error(f"[{market['name']}] エラー: {e}", exc_info=True)
            time.sleep(61)
        else:
            time.sleep(config.POLL_INTERVAL_SEC)


# ─── エントリーポイント ───────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--us-now" in args:
        run(config.US_MARKET)

    elif "--jp-now" in args:
        run(config.JP_MARKET)

    else:
        # 両市場を別スレッドでスケジュール待機
        logger.info("=" * 55)
        logger.info("  TradingView 自動スクリーニング起動")
        logger.info(f"  🇺🇸 米国株: 毎朝 {config.US_MARKET['schedule_hour']:02d}:{config.US_MARKET['schedule_minute']:02d}")
        logger.info(f"  🇯🇵 日本株: 毎日 {config.JP_MARKET['schedule_hour']:02d}:{config.JP_MARKET['schedule_minute']:02d}")
        logger.info("=" * 55)

        t_us = threading.Thread(
            target=market_scheduler,
            args=(config.US_MARKET,),
            daemon=True,
            name="US-Scheduler",
        )
        t_jp = threading.Thread(
            target=market_scheduler,
            args=(config.JP_MARKET,),
            daemon=True,
            name="JP-Scheduler",
        )
        t_us.start()
        t_jp.start()

        # メインスレッドは Ctrl+C まで待機
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("スクリーニングシステムを停止しました。")
